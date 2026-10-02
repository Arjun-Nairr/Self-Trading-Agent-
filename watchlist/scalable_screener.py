"""
scalable_screener.py -- Stage 1 of docs/scopes/SCALABLE_SCREENER_V1.md.

A PARALLEL, live/current-universe screener. It does not replace, import, or
alter screen_watchlist.py, check_news_coverage.py, or watchlist.json (the
fixed 10-name path), and nothing in the backtest harness, the risk checker,
Hermes, or any Laya module imports it.

    current S&P MidCap 400 universe
      -> deterministic basic eligibility checks (RULES.md §8 thresholds)
      -> variable eligible candidate pool (no target size, no sector trim)
      -> immutable local screen records

Every universe name gets exactly one status -- `eligible`, `rejected`, or
`data_unavailable` -- with ordered machine-readable reason codes. Checks run
in cost order (company facts, then history, then news) and a name stops at the
first stage that decides it, so the expensive fetches only happen for names
still in play.

CURRENT DATA ONLY. The live providers return today's membership and today's
company facts, so a live screen may only be recorded under TODAY's UTC date --
never a past or future one -- and must never feed a historical backtest
(scope §5).

Records (all write-once; nothing is ever edited, there is no "current" pointer):

    <dir>/attempts/<screen id>.a<NNN>.json   every attempt, successful or failed
    <dir>/results/<screen id>.json           the ONE successful attempt for that
                                             logical screen (byte-identical copy)

    logical screen id = screen date + config hash

CLI (needs the maverick-mcp and alpaca-mcp containers; there is no scheduler):

    python watchlist/scalable_screener.py --dry-run     # today (UTC), any weekday; writes nothing
    python watchlist/scalable_screener.py --write       # today (UTC), Saturday/Sunday only; records the attempt
    python watchlist/scalable_screener.py --usable --on-date 2026-10-12   # no fetch; any date
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

REPO = Path(__file__).resolve().parent.parent
# Runtime data, git-ignored -- same convention as backtest-harness/.selector_snapshots/.
SNAPSHOT_DIR = REPO / "watchlist" / ".screen_snapshots"

SCREENER_VERSION = "scalable-screener-v1-stage1.2"
SCHEMA_VERSION = 3

STATUS_ELIGIBLE = "eligible"
STATUS_REJECTED = "rejected"
STATUS_DATA_UNAVAILABLE = "data_unavailable"
STATUSES = (STATUS_ELIGIBLE, STATUS_REJECTED, STATUS_DATA_UNAVAILABLE)
SCREEN_OK = "ok"
SCREEN_FAILED = "failed"
STAGES = ("facts", "history", "news")

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
    "filter_order": list(STAGES),
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

# Live request limits per stage. Deliberately conservative; they bound the
# workload but do not change what "eligible" means, so they are recorded in
# telemetry rather than in the config hash.
DEFAULT_LIMITS: dict[str, dict[str, float]] = {
    "universe": {"concurrency": 1, "attempts": 3, "timeout_s": 45, "backoff_base_s": 2, "backoff_cap_s": 8, "min_interval_s": 0},
    "facts":    {"concurrency": 4, "attempts": 3, "timeout_s": 30, "backoff_base_s": 1, "backoff_cap_s": 8, "min_interval_s": 0.25},
    "history":  {"concurrency": 4, "attempts": 3, "timeout_s": 30, "backoff_base_s": 1, "backoff_cap_s": 8, "min_interval_s": 0.25},
    "news":     {"concurrency": 2, "attempts": 3, "timeout_s": 60, "backoff_base_s": 1, "backoff_cap_s": 8, "min_interval_s": 0.5},
}
LIMIT_BOUNDS = {"concurrency": (1, 8), "attempts": (1, 5), "timeout_s": (1, 120)}
MAX_ERROR_CODES = 10


class ScreenConflict(RuntimeError):
    """A DIFFERENT record already exists where a write-once record was about
    to be written. Nothing is ever overwritten."""


class SnapshotCorrupt(ValueError):
    """A stored record failed validation."""


class LiveDateError(ValueError):
    """A live (current-data) screen was requested for a date it may not use."""


class ProviderError(RuntimeError):
    """A whole provider stage failed. `code` is a short sanitized label;
    `stats` optionally carries the request/retry counts spent before failing."""

    def __init__(self, code: str, stats: Any = None):
        super().__init__(code)
        self.code, self.stats = code, stats


class Providers(Protocol):
    """The four data boundaries. Each returns plain JSON-able data; raising
    means the whole stage could not be completed. An optional `stats`
    ({"requests": int, "retries": int}) is copied into telemetry. A provider
    with `live = True` returns CURRENT data and is date-locked to the run's
    start date."""

    def universe(self) -> Mapping[str, Any]:
        """-> {"source", "url", "symbols": [...]}"""

    def facts(self, symbols: Sequence[str]) -> Mapping[str, Any]:
        """-> {"rows": {symbol: {"name", "sector", "industry", "market_cap",
        "avg_volume", "price"} | {"error": code}}}"""

    def history(self, symbols: Sequence[str]) -> Mapping[str, Any]:
        """-> {"rows": {symbol: {"bars": int} | {"error": code}}}"""

    def news(self, symbols: Sequence[str], windows: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        """-> {"rows": {symbol: [{"articles": int} | {"error": code}, ... one
        per window, same order]}}"""


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
    """The LOGICAL screen: deterministic from the screen date and config only."""
    return f"{screen_date.isoformat()}_{config_hash(config)[:12]}"


def attempt_id(sid: str, attempt: int) -> str:
    return f"{sid}.a{attempt:03d}"


def snapshot_to_json(snap: Mapping[str, Any]) -> str:
    """Byte-stable: sorted keys, ASCII, no NaN, trailing newline."""
    return json.dumps(snap, sort_keys=True, indent=1, ensure_ascii=True, allow_nan=False) + "\n"


def _content_hash(snap: Mapping[str, Any]) -> str:
    return _hash({k: v for k, v in snap.items() if k != "content_hash"})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_CODE_RE = re.compile(r"[A-Za-z0-9_]{1,40}")


def _is_code(value: Any) -> bool:
    return isinstance(value, str) and _CODE_RE.fullmatch(value) is not None


def _code(value: Any) -> str:
    """Only a short identifier ever reaches a record -- never an exception
    message, traceback, URL, or remote payload."""
    return value if _is_code(value) else "error"


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
    ponytail: weekly cadence and no market calendar -- a Monday holiday still
    counts as the start of the week; use the exchange calendar (and generalise
    for a non-weekly interval) if either ever matters."""
    return screen_date + timedelta(days=7 - screen_date.weekday())


def require_today(screen_date: date, today: date) -> None:
    if screen_date != today:
        raise LiveDateError(
            f"a live screen uses CURRENT membership and facts, so it can only be recorded under "
            f"the UTC date the run started on ({today}); got {screen_date}. Past dates are readable "
            f"with --usable --on-date; a current screen is never stored under another date.")


def require_weekend(screen_date: date) -> None:
    if screen_date.weekday() < 5:
        raise LiveDateError(
            f"--write records the weekly refresh, which runs on Saturday or Sunday (UTC) and takes "
            f"effect the following Monday; {screen_date} is a {screen_date.strftime('%A')}. "
            f"Use --dry-run to validate on a weekday (it writes nothing).")


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _text(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _is_error(entry: Any) -> bool:
    return not isinstance(entry, Mapping) or "error" in entry


_FACT_KEYS = ("name", "sector", "industry", "market_cap", "avg_volume", "price")


def evaluate_facts(entry: Any, config: Mapping[str, Any]) -> tuple[dict[str, Any], str | None, list[str]]:
    """-> (sanitized raw inputs, deciding status or None to continue, reasons)."""
    if _is_error(entry):
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
    if _is_error(entry):
        reason = "history_fetch_error" if isinstance(entry, Mapping) else "history_missing"
        return None, STATUS_DATA_UNAVAILABLE, [reason]
    bars = entry.get("bars")
    if isinstance(bars, bool) or not isinstance(bars, int) or bars < 0:
        return None, STATUS_DATA_UNAVAILABLE, ["history_missing"]
    if bars < config["min_history_bars"]:
        return bars, STATUS_REJECTED, ["history_below_min_bars"]
    return bars, None, []


def _probe_count(probe: Any) -> int | None:
    n = probe.get("articles") if isinstance(probe, Mapping) else None
    return None if isinstance(n, bool) or not isinstance(n, int) or n < 0 else n


def evaluate_news(entry: Any, n_windows: int, config: Mapping[str, Any]) -> tuple[list[int | None] | None, str, list[str]]:
    """A probe that errored (including incomplete pagination) is unproven
    coverage, not zero coverage."""
    if not isinstance(entry, (list, tuple)) or len(entry) != n_windows:
        reason = "news_fetch_error" if isinstance(entry, Mapping) else "news_missing"
        return None, STATUS_DATA_UNAVAILABLE, [reason]
    counts = [_probe_count(p) for p in entry]
    reasons = [f"news_probe_{i}_unavailable" for i, n in enumerate(counts, start=1) if n is None]
    if reasons:
        return counts, STATUS_DATA_UNAVAILABLE, reasons
    if sum(counts) < config["min_news_articles"]:
        return counts, STATUS_REJECTED, ["news_coverage_below_min"]
    return counts, STATUS_ELIGIBLE, []


# --------------------------------------------------------------------------
# the screen
# --------------------------------------------------------------------------

def _stage_telemetry(name: str, pending: Sequence[str], rows: Mapping[str, Any], stats: Any,
                     started: datetime, finished: datetime, stage_error: str | None) -> dict[str, Any]:
    """The one telemetry shape every stage (universe included) records."""
    errors: dict[str, int] = {}
    ok = 0
    for s in pending:
        entry = rows.get(s)
        probes = list(entry) if name == "news" and isinstance(entry, (list, tuple)) else [entry]
        bad = [p for p in probes if _is_error(p)]
        ok += not bad
        for p in bad:
            code = _code(p.get("error")) if isinstance(p, Mapping) else "missing"
            errors[code] = errors.get(code, 0) + 1
    kept = dict(sorted(errors.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_ERROR_CODES])
    if len(errors) > len(kept):
        kept["other"] = sum(errors.values()) - sum(kept.values())

    def stat(key: str) -> int | None:
        v = stats.get(key) if isinstance(stats, Mapping) else None
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    return {"started_at": started.isoformat(), "elapsed_s": round((finished - started).total_seconds(), 3),
            "requested": len(pending), "ok": ok, "unavailable": len(pending) - ok, "errors": kept,
            "requests": stat("requests"), "retries": stat("retries"), "stage_error": stage_error}


def run_screen(
    screen_date: date, providers: Providers, *,
    config: Mapping[str, Any] = DEFAULT_CONFIG, snapshot_dir: Path | None = None,
    attempt: int = 1, clock: Callable[[], datetime] = _utc_now, started: datetime | None = None,
) -> dict[str, Any]:
    """Build one attempt record (ok or failed). Touches disk only to READ the
    previous successful screen reference.

    `started` is the single run-start instant (captured once by the caller, or
    here). A live provider is refused -- before any fetch -- unless
    `screen_date` is that instant's UTC date. The date is never re-checked, so
    a run accepted at 23:59 UTC completes normally after midnight. Injected
    fixtures may use any date. Identical provider outputs, clock, and directory
    state give an identical record."""
    started = started or clock()
    if getattr(providers, "live", False):
        require_today(screen_date, started.astimezone(timezone.utc).date())

    # Lineage before any fetch: only a fully validated result with its backing
    # attempt can be named as the predecessor (an invalid record raises here).
    previous = None
    if snapshot_dir is not None:
        earlier = [r for r in _results(snapshot_dir, config_hash(config))
                   if r["screen_date"] < screen_date.isoformat()]
        if earlier:
            last = max(earlier, key=lambda r: r["screen_date"])
            previous = {"screen_id": last["screen_id"], "content_hash": last["content_hash"]}

    windows = news_windows(screen_date, config)
    failures: list[str] = []
    telemetry: dict[str, Any] = {"limits": _limits_record(getattr(providers, "limits", None))}

    universe = {"source": UNIVERSE_SOURCE, "url": UNIVERSE_URL, "fetched_at": None,
                "count": 0, "constituents_hash": None}
    symbols: list[str] = []
    t0, u_stats, u_error = clock(), None, None
    try:
        u = providers.universe()
        u_stats = u.get("stats")
        symbols = sorted({s.strip() for s in u["symbols"] if isinstance(s, str) and s.strip()})
        universe = {"source": str(u["source"]), "url": str(u["url"]), "fetched_at": clock().isoformat(),
                    "count": len(symbols), "constituents_hash": _hash(symbols) if symbols else None}
        u_entry: dict[str, Any] = {"count": len(symbols)}
        if not symbols:
            failures.append("universe_empty")
            u_entry = {"error": "empty"}
    except Exception as exc:
        u_error = _code(getattr(exc, "code", type(exc).__name__))
        u_stats = getattr(exc, "stats", None)
        failures.append("universe_fetch_failed")
        u_entry = {"error": u_error}
    telemetry["universe"] = _stage_telemetry("universe", ["universe"], {"universe": u_entry}, u_stats,
                                             t0, clock(), u_error)

    rows = {s: {"symbol": s, "name": None, "sector": None, "industry": None, "market_cap": None,
                "avg_volume": None, "price": None, "history_bars": None, "news_counts": None,
                "status": None, "reasons": []} for s in symbols}

    def stage(name: str, pending: list[str], fetch) -> dict[str, Any]:
        """Run one provider stage; a raise fails the screen and leaves every
        pending name data_unavailable (visible, never dropped)."""
        t0, got, stats, error = clock(), {}, None, None
        if pending:
            try:
                result = fetch(pending)
                got, stats = dict(result["rows"]), result.get("stats")
            except Exception as exc:
                error = _code(getattr(exc, "code", type(exc).__name__))
                stats = getattr(exc, "stats", None)
                failures.append("news_coverage_incomplete" if name == "news" else f"{name}_provider_failed")
                got = {s: {"error": "stage_failed"} for s in pending}
        telemetry[name] = _stage_telemetry(name, pending, got, stats, t0, clock(), error)
        return got

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

    counts = {k: sum(1 for r in rows.values() if r["status"] == k) for k in STATUSES}
    if symbols and counts[STATUS_DATA_UNAVAILABLE] / len(symbols) > config["max_data_unavailable_fraction"]:
        failures.append("data_unavailable_above_threshold")

    ok = not failures
    pool = sorted(s for s, r in rows.items() if r["status"] == STATUS_ELIGIBLE) if ok else None

    sid = screen_id(screen_date, config)
    snap = {
        "schema_version": SCHEMA_VERSION, "screener_version": SCREENER_VERSION,
        "screen_id": sid, "attempt": attempt, "attempt_id": attempt_id(sid, attempt),
        "screen_date": screen_date.isoformat(),
        "started_at": started.isoformat(), "finished_at": clock().isoformat(),
        "config": dict(config), "config_hash": config_hash(config),
        "universe": universe, "news_windows": windows, "telemetry": telemetry,
        "rows": [rows[s] for s in symbols], "counts": counts,
        "pool": pool, "pool_hash": _hash(pool) if ok else None,
        "sector_mix": _sector_mix(rows[s] for s in pool) if ok else None,
        "screen_status": SCREEN_OK if ok else SCREEN_FAILED, "failure_reasons": failures,
        "effective_from": effective_from(screen_date).isoformat() if ok else None,
        "previous_ok_screen": previous,
    }
    snap["content_hash"] = _content_hash(snap)
    return snap


def _sector_mix(eligible_rows) -> dict[str, int]:
    mix: dict[str, int] = {}
    for r in eligible_rows:
        sector = r["sector"] or "Unknown"
        mix[sector] = mix.get(sector, 0) + 1
    return mix


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

_NONE = type(None)
_TYPES: dict[str, type | tuple[type, ...]] = {
    "schema_version": int, "screener_version": str, "screen_id": str, "attempt": int, "attempt_id": str,
    "screen_date": str, "started_at": str, "finished_at": str, "config": dict, "config_hash": str,
    "universe": dict, "news_windows": list, "telemetry": dict, "rows": list, "counts": dict,
    "pool": (list, _NONE), "pool_hash": (str, _NONE), "sector_mix": (dict, _NONE),
    "screen_status": str, "failure_reasons": list, "effective_from": (str, _NONE),
    "previous_ok_screen": (dict, _NONE), "content_hash": str,
}
_ROW_KEYS = {"symbol", "name", "sector", "industry", "market_cap", "avg_volume", "price",
             "history_bars", "news_counts", "status", "reasons"}
_UNIVERSE_KEYS = {"source", "url", "fetched_at", "count", "constituents_hash"}
_STAGE_KEYS = {"started_at", "elapsed_s", "requested", "ok", "unavailable", "errors",
               "requests", "retries", "stage_error"}
_SID_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_([0-9a-f]{12})")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")


def _is_count(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_amount(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _stamp(v: Any) -> datetime | None:
    """A timezone-aware ISO timestamp, or None."""
    try:
        t = datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo is not None and t.utcoffset() is not None else None


def _recheck_row(r: Mapping[str, Any], config: Mapping[str, Any], n_windows: int) -> tuple[str, str, list[list[str]]]:
    """Re-derive a row's outcome from its own recorded inputs.
    -> (stage that decided it, status, acceptable reason lists). A stored
    status/reason that the stored numbers do not produce is a forgery."""
    facts = {k: r[k] for k in _FACT_KEYS}
    _, status, why = evaluate_facts(facts, config)
    if all(v is None for v in facts.values()):  # nothing came back at all
        return "facts", STATUS_DATA_UNAVAILABLE, [["facts_fetch_error"], ["facts_missing"], why]
    if status is not None:
        return "facts", status, [why]
    if r["history_bars"] is None:
        return "history", STATUS_DATA_UNAVAILABLE, [["history_fetch_error"], ["history_missing"]]
    _, status, why = evaluate_history({"bars": r["history_bars"]}, config)
    if status is not None:
        return "history", status, [why]
    if r["news_counts"] is None:
        return "news", STATUS_DATA_UNAVAILABLE, [["news_fetch_error"], ["news_missing"]]
    _, status, why = evaluate_news([{"articles": c} for c in r["news_counts"]], n_windows, config)
    return "news", status, [why]


def validate_snapshot(snap: Any, *, kind: str, stem: str | None = None) -> dict[str, Any]:
    """Everything a record must satisfy before it is reused or treated as
    usable. A self-consistent content hash alone proves nothing -- anyone can
    re-hash an edited file -- so every nested value is type-checked and every
    conclusion (row status, counts, pool, sector mix, failure state) is
    re-derived from the record's own evidence. `kind` is "attempt" or
    "result"; `stem` is the filename without its extension."""
    def need(cond: Any, why: str) -> None:
        if not cond:
            raise SnapshotCorrupt(f"{stem or 'snapshot'}: {why}")

    need(isinstance(snap, dict), "not a JSON object")
    need(snap.get("schema_version") == SCHEMA_VERSION, f"unsupported schema_version {snap.get('schema_version')!r}")
    need(set(snap) == set(_TYPES), f"unexpected or missing keys {sorted(set(snap) ^ set(_TYPES))}")
    for key, typ in _TYPES.items():
        need(isinstance(snap[key], typ) and not isinstance(snap[key], bool), f"{key} has the wrong type")
    need(snap["content_hash"] == _content_hash(snap), "content hash mismatch -- the record was altered")

    # identity ----------------------------------------------------------
    config = snap["config"]
    need(set(config) == set(DEFAULT_CONFIG), "config keys are not the screener's config keys")
    for key, default in DEFAULT_CONFIG.items():
        v = config[key]
        if isinstance(default, list):
            item_ok = _is_count if key == "news_probe_offsets_days" else (lambda x: isinstance(x, str))
            need(isinstance(v, list) and all(item_ok(x) for x in v), f"config {key} malformed")
        elif isinstance(default, str):
            need(isinstance(v, str), f"config {key} malformed")
        else:
            need(_is_amount(v), f"config {key} malformed")
    need(_is_count(config["refresh_interval_days"]) and config["refresh_interval_days"] >= 1
         and _is_count(config["carry_forward_refreshes"]) and _is_count(config["news_lookback_days"]),
         "config cadence values malformed")
    try:
        day = date.fromisoformat(snap["screen_date"])
    except ValueError:
        need(False, "invalid screen_date")
    need(snap["config_hash"] == config_hash(config), "config_hash does not match config")
    need(snap["screen_id"] == screen_id(day, config), "screen_id does not match date + config")
    need(snap["attempt"] >= 1 and snap["attempt_id"] == attempt_id(snap["screen_id"], snap["attempt"]),
         "attempt_id does not match screen_id + attempt")
    need(stem is None or stem == (snap["attempt_id"] if kind == "attempt" else snap["screen_id"]),
         "filename does not match the record's own ID")
    need(isinstance(snap["screener_version"], str) and snap["screener_version"], "screener_version missing")

    # timestamps --------------------------------------------------------
    started, finished = _stamp(snap["started_at"]), _stamp(snap["finished_at"])
    need(started and finished, "started_at / finished_at are not timezone-aware timestamps")
    need(started <= finished, "finished_at is before started_at")

    def in_run(v: Any) -> bool:
        t = _stamp(v)
        return t is not None and started <= t <= finished

    need(snap["news_windows"] == news_windows(day, config), "news windows are not the ones this date and config produce")
    n_windows = len(snap["news_windows"])

    # rows --------------------------------------------------------------
    symbols, decided_at = [], []
    for r in snap["rows"]:
        need(isinstance(r, dict) and set(r) == _ROW_KEYS, "malformed row")
        need(isinstance(r["symbol"], str) and r["symbol"] and r["status"] in STATUSES, "row has no valid symbol/status")
        need(all(r[k] is None or (isinstance(r[k], str) and r[k]) for k in ("name", "sector", "industry")),
             "row text field malformed")
        need(all(r[k] is None or _is_amount(r[k]) for k in ("market_cap", "avg_volume", "price")),
             "row numeric fact is not a finite non-negative number")
        need(r["history_bars"] is None or _is_count(r["history_bars"]), "row history_bars malformed")
        nc = r["news_counts"]
        need(nc is None or (isinstance(nc, list) and len(nc) == n_windows
                            and all(c is None or _is_count(c) for c in nc)), "row news_counts malformed")
        need(isinstance(r["reasons"], list) and all(_is_code(x) for x in r["reasons"]), "row reasons malformed")
        stage_name, status, acceptable = _recheck_row(r, config, n_windows)
        need(r["status"] == status and r["reasons"] in acceptable,
             "row status/reasons are not what its recorded inputs produce")
        need(stage_name == "news" or r["news_counts"] is None, "row carries news it should never have fetched")
        need(stage_name != "facts" or r["history_bars"] is None, "row carries history it should never have fetched")
        symbols.append(r["symbol"])
        decided_at.append(stage_name)
    need(symbols == sorted(set(symbols)), "row symbols are not sorted and unique")
    counts = {k: sum(1 for r in snap["rows"] if r["status"] == k) for k in STATUSES}
    need(snap["counts"] == counts, "counts do not match rows")

    # telemetry ---------------------------------------------------------
    tel = snap["telemetry"]
    need(set(tel) == {"limits", "universe", *STAGES}, "telemetry stages missing or unexpected")
    lim = tel["limits"]
    need(lim is None or (isinstance(lim, dict) and all(
        isinstance(v, dict) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v.values())
        for v in lim.values())), "telemetry limits malformed")
    for name in ("universe", *STAGES):
        t = tel[name]
        need(isinstance(t, dict) and set(t) == _STAGE_KEYS, f"{name} telemetry malformed")
        need(in_run(t["started_at"]), f"{name} telemetry timestamp is outside the run")
        need(_is_amount(t["elapsed_s"]), f"{name} telemetry elapsed time malformed")
        need(all(_is_count(t[k]) for k in ("requested", "ok", "unavailable"))
             and t["ok"] + t["unavailable"] == t["requested"], f"{name} telemetry counters do not add up")
        need(all(t[k] is None or _is_count(t[k]) for k in ("requests", "retries")), f"{name} telemetry request counts malformed")
        need(isinstance(t["errors"], dict) and len(t["errors"]) <= MAX_ERROR_CODES + 1
             and all(_is_code(k) and _is_count(v) and v >= 1 for k, v in t["errors"].items()),
             f"{name} telemetry errors are not bounded short codes")
        need(t["stage_error"] is None or _is_code(t["stage_error"]), f"{name} telemetry stage_error malformed")
    need(tel["universe"]["requested"] == 1 and tel["universe"]["ok"] == (1 if symbols else 0),
         "universe telemetry does not match the universe")
    need(tel["facts"]["requested"] == len(symbols)
         and tel["history"]["requested"] == sum(1 for s in decided_at if s != "facts")
         and tel["news"]["requested"] == sum(1 for s in decided_at if s == "news"),
         "stage telemetry does not match the rows")

    # universe ----------------------------------------------------------
    u = snap["universe"]
    need(set(u) == _UNIVERSE_KEYS, "universe keys malformed")
    need(all(isinstance(u[k], str) and u[k] for k in ("source", "url")), "universe source/url malformed")
    need(_is_count(u["count"]) and u["count"] == len(symbols), "universe count does not match rows")
    need(u["constituents_hash"] == (_hash(symbols) if symbols else None), "constituents hash mismatch")
    fetch_failed = tel["universe"]["stage_error"] is not None
    need(not (fetch_failed and symbols), "a failed universe fetch cannot have rows")
    need((u["fetched_at"] is None) if fetch_failed else in_run(u["fetched_at"]),
         "universe fetched_at is missing, not timezone-aware, or outside the run")

    # outcome: re-derived from the evidence, never taken on trust ---------
    derived = ["universe_fetch_failed"] if fetch_failed else ([] if symbols else ["universe_empty"])
    derived += ["news_coverage_incomplete" if name == "news" else f"{name}_provider_failed"
                for name in STAGES if tel[name]["stage_error"] is not None]
    if symbols and counts[STATUS_DATA_UNAVAILABLE] / len(symbols) > config["max_data_unavailable_fraction"]:
        derived.append("data_unavailable_above_threshold")
    status = snap["screen_status"]
    need(status in (SCREEN_OK, SCREEN_FAILED), "invalid screen_status")
    need(snap["failure_reasons"] == derived, "failure_reasons do not match the recorded evidence")
    need((status == SCREEN_OK) == (not derived), "screen_status does not match the recorded evidence")
    if status == SCREEN_OK:
        eligible = [r for r in snap["rows"] if r["status"] == STATUS_ELIGIBLE]
        names = [r["symbol"] for r in eligible]
        need(snap["pool"] == names, "pool is not exactly the sorted eligible rows")
        need(snap["pool_hash"] == _hash(names), "pool hash mismatch")
        mix = snap["sector_mix"]
        need(isinstance(mix, dict) and all(isinstance(k, str) and _is_count(v) and v >= 1 for k, v in mix.items())
             and mix == _sector_mix(eligible), "sector mix does not match the eligible rows")
        need(snap["effective_from"] == effective_from(day).isoformat(), "effective_from is not the following Monday")
    else:
        need(all(snap[k] is None for k in ("pool", "pool_hash", "sector_mix", "effective_from")),
             "a failed attempt must not carry a pool or an effective date")
    need(kind == "attempt" or status == SCREEN_OK, "a result record must be a successful screen")

    # lineage -----------------------------------------------------------
    prev = snap["previous_ok_screen"]
    if prev is not None:
        need(set(prev) == {"screen_id", "content_hash"}, "previous_ok_screen malformed")
        m = _SID_RE.fullmatch(prev["screen_id"]) if isinstance(prev["screen_id"], str) else None
        need(m and m.group(2) == snap["config_hash"][:12] and m.group(1) < snap["screen_date"],
             "previous_ok_screen is not an earlier screen of this config")
        need(isinstance(prev["content_hash"], str) and _HEX64_RE.fullmatch(prev["content_hash"]),
             "previous_ok_screen content hash malformed")
    return snap


# --------------------------------------------------------------------------
# write-once persistence: attempts and results
# --------------------------------------------------------------------------

def attempts_dir(snapshot_dir: Path) -> Path:
    return Path(snapshot_dir) / "attempts"


def results_dir(snapshot_dir: Path) -> Path:
    return Path(snapshot_dir) / "results"


def attempt_path(snapshot_dir: Path, aid: str) -> Path:
    return attempts_dir(snapshot_dir) / f"{aid}.json"


def result_path(snapshot_dir: Path, sid: str) -> Path:
    return results_dir(snapshot_dir) / f"{sid}.json"


def load_snapshot(path: Path, kind: str) -> dict[str, Any]:
    path = Path(path)
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SnapshotCorrupt(f"{path.name}: not valid JSON") from exc
    return validate_snapshot(snap, kind=kind, stem=path.stem)


def load_result(snapshot_dir: Path, sid: str) -> dict[str, Any]:
    """The successful record for a logical screen: fully validated AND backed
    by its own validated, byte-identical attempt record. An orphaned result
    (no such attempt) is not a result."""
    path = result_path(snapshot_dir, sid)
    snap = load_snapshot(path, "result")
    source = attempt_path(snapshot_dir, snap["attempt_id"])
    if not source.exists():
        raise SnapshotCorrupt(f"{path.name}: orphaned result -- no attempt record {source.name}")
    load_snapshot(source, "attempt")
    if source.read_bytes() != path.read_bytes():
        raise SnapshotCorrupt(f"{path.name}: differs from its attempt record {source.name}")
    return snap


def _results(snapshot_dir: Path, cfg_hash: str) -> list[dict[str, Any]]:
    """Every stored result, each through load_result. Raises on any invalid one."""
    d = results_dir(snapshot_dir)
    if not d.is_dir():
        return []
    return [s for s in (load_result(snapshot_dir, p.stem) for p in sorted(d.glob("*.json")))
            if s["config_hash"] == cfg_hash]


def _attempts(snapshot_dir: Path, cfg_hash: str) -> list[dict[str, Any]]:
    d = attempts_dir(snapshot_dir)
    if not d.is_dir():
        return []
    return [s for s in (load_snapshot(p, "attempt") for p in sorted(d.glob("*.json")))
            if s["config_hash"] == cfg_hash]


def _write_once(path: Path, text: str) -> None:
    """Atomic and exclusive: temp file + os.link, which refuses to replace an
    existing file. Identical content is a no-op; different content raises
    FileExistsError and leaves the stored file untouched."""
    def same_or_raise() -> None:
        if path.read_text(encoding="utf-8") != text:
            raise FileExistsError(str(path))

    if path.exists():
        return same_or_raise()
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
            same_or_raise()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def next_attempt(snapshot_dir: Path, sid: str) -> int:
    used = [int(p.stem.rsplit(".a", 1)[1]) for p in attempts_dir(snapshot_dir).glob(f"{sid}.a[0-9][0-9][0-9].json")]
    return max(used, default=0) + 1


def write_attempt(snapshot_dir: Path, snap: Mapping[str, Any]) -> Path:
    validate_snapshot(dict(snap), kind="attempt")
    path = attempt_path(snapshot_dir, snap["attempt_id"])
    try:
        _write_once(path, snapshot_to_json(snap))
    except FileExistsError:
        raise ScreenConflict(f"{path} already holds a different attempt; attempts are write-once") from None
    return path


def publish_result(snapshot_dir: Path, snap: Mapping[str, Any]) -> Path:
    """Make a successful attempt THE result of its logical screen. A logical
    screen has at most one result, forever: a different successful attempt
    for the same screen fails loudly and replaces nothing."""
    validate_snapshot(dict(snap), kind="result")
    path = result_path(snapshot_dir, snap["screen_id"])
    try:
        _write_once(path, snapshot_to_json(snap))
    except FileExistsError:
        raise ScreenConflict(
            f"{path} already holds a different successful result for screen {snap['screen_id']}; "
            f"attempt {snap['attempt_id']} is kept as an attempt but is NOT the usable result") from None
    return path


def screen(
    screen_date: date, providers: Providers, snapshot_dir: Path, *,
    config: Mapping[str, Any] = DEFAULT_CONFIG, clock: Callable[[], datetime] = _utc_now,
    started: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """If this logical screen already has a result: validate and return it
    WITHOUT fetching. Otherwise run a new attempt, record it (after earlier
    failed attempts, never over them), and publish it as the result if it
    succeeded. A LIVE provider may only be persisted on the run's own start
    date, and only on a Saturday or Sunday (the weekly refresh).
    -> (record, reused)."""
    started = started or clock()
    if getattr(providers, "live", False):
        require_today(screen_date, started.astimezone(timezone.utc).date())
        require_weekend(screen_date)
    sid = screen_id(screen_date, config)
    if result_path(snapshot_dir, sid).exists():
        return load_result(snapshot_dir, sid), True
    snap = run_screen(screen_date, providers, config=config, snapshot_dir=snapshot_dir,
                      attempt=next_attempt(snapshot_dir, sid), clock=clock, started=started)
    for _ in range(50):  # a concurrent writer took this number: take the next one
        try:
            write_attempt(snapshot_dir, snap)
            break
        except ScreenConflict:
            snap["attempt"] += 1
            snap["attempt_id"] = attempt_id(sid, snap["attempt"])
            snap["content_hash"] = _content_hash(snap)
    else:
        raise ScreenConflict(f"could not allocate an attempt number for {sid}")
    if snap["screen_status"] == SCREEN_OK:
        publish_result(snapshot_dir, snap)
    return snap, False


def usable_pool(
    snapshot_dir: Path, on_date: date, *, config: Mapping[str, Any] = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Which pool, if any, may be used for NEW entries on `on_date`. Reads
    stored records only; fails closed.

    The latest successful result already in effect is `current` during its
    own week. After that it is `carried_forward` ONLY while each refresh that
    should have replaced it has a recorded FAILED attempt, and for at most
    `carry_forward_refreshes` refreshes. A refresh nobody attempted is not a
    failure: with no evidence the pool is `unavailable`."""
    def out(state: str, reason: str | None, snap: Mapping[str, Any] | None = None, evidence: Sequence[str] = ()):
        usable = state in ("current", "carried_forward")
        return {"state": state, "reason": reason, "screen_id": snap["screen_id"] if snap else None,
                "pool": list(snap["pool"]) if usable else None, "evidence": list(evidence)}

    cfg_hash = config_hash(config)
    try:
        results = _results(snapshot_dir, cfg_hash)
        attempts = _attempts(snapshot_dir, cfg_hash)
    except (SnapshotCorrupt, OSError):
        return out("unavailable", "snapshot_invalid")
    live = [s for s in results if s["effective_from"] <= on_date.isoformat()]
    if not live:
        return out("unavailable", "no_successful_screen")
    snap = max(live, key=lambda s: (s["effective_from"], s["screen_date"]))
    start = date.fromisoformat(snap["effective_from"])
    interval = snap["config"]["refresh_interval_days"]
    missed = (on_date - start).days // interval
    if missed == 0:
        return out("current", None, snap)
    if missed > snap["config"]["carry_forward_refreshes"]:
        return out("unavailable", "last_successful_screen_expired", snap)
    evidence = []
    for k in range(1, missed + 1):
        target = (start + timedelta(days=k * interval)).isoformat()
        failed = [a["attempt_id"] for a in attempts if a["screen_status"] == SCREEN_FAILED
                  and effective_from(date.fromisoformat(a["screen_date"])).isoformat() == target]
        if not failed:
            return out("unavailable", "refresh_not_attempted", snap)
        evidence.append(max(failed))
    return out("carried_forward", None, snap, evidence)


# --------------------------------------------------------------------------
# bounded live fetching
# --------------------------------------------------------------------------

def bounded_map(items, child_source, *, concurrency, attempts, timeout_s, backoff_base_s, backoff_cap_s,
                min_interval_s=0.0, deadline_s=None, sleep=None):
    """Run `child_source` (Python source defining `one(item)`) once per item,
    EVERY REQUEST IN ITS OWN CHILD PROCESS.

    Why a process: a thread cannot be cancelled, so a "timed-out" request in a
    thread keeps running while its slot is reused. A child process can be
    killed. Each of the `concurrency` workers runs one child at a time and
    does not return from a request until that child has exited or been killed
    AND reaped, so mechanically:
      * never more than `concurrency` underlying requests exist at once;
      * a timed-out request is dead before its slot is reused or retried;
      * a retry cannot overlap the earlier try of the same item.
    Also: at least `min_interval_s` between request starts, at most `attempts`
    tries per item with capped exponential backoff, and no request starts
    after `deadline_s` -- so the whole stage ends within deadline_s + timeout_s
    even if the upstream library hangs.

    -> ({item: {"ok": value} | {"error": code}}, {"requests": n, "retries": n}).
    `code` is the exception CLASS NAME, "timeout", "child_failed", or
    "stage_deadline" -- never a message. Items must be strings. Self-contained
    on purpose: this exact source is shipped into the provider containers."""
    import json
    import re
    import subprocess
    import sys
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    mark = "CHILD_JSON:"
    runner = child_source + "\n" + "\n".join([
        "import json as _json, sys as _sys",
        "try:",
        "    _result = {'ok': one(_sys.argv[1])}",
        "except BaseException as _exc:",
        "    _result = {'error': type(_exc).__name__}",
        "print(" + repr(mark) + " + _json.dumps(_result), flush=True)",
    ])
    sleep = sleep or time.sleep
    attempts = max(1, int(attempts))
    stop_at = None if deadline_s is None else time.monotonic() + deadline_s
    lock = threading.Lock()
    state = {"next_start": 0.0, "requests": 0, "retries": 0}

    def pace():
        with lock:
            now = time.monotonic()
            wait = state["next_start"] - now
            state["next_start"] = max(now, state["next_start"]) + min_interval_s
            state["requests"] += 1
        if wait > 0:
            sleep(wait)

    def call(item):
        proc = subprocess.Popen([sys.executable, "-c", runner, item], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            out, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()  # reaped: the request no longer exists when this worker moves on
            return {"error": "timeout"}
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        for line in reversed((out or "").splitlines()):
            if line.startswith(mark):
                try:
                    result = json.loads(line[len(mark):])
                except ValueError:
                    break
                if isinstance(result, dict) and "ok" in result:
                    return {"ok": result["ok"]}
                code = result.get("error") if isinstance(result, dict) else None
                ok_code = isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,40}", code)
                return {"error": code if ok_code else "error"}
        return {"error": "child_failed"}

    def work(item):
        result = {"error": "stage_deadline"}
        for n in range(attempts):
            if n:
                with lock:
                    state["retries"] += 1
                sleep(min(backoff_cap_s, backoff_base_s * 2 ** (n - 1)))
            if stop_at is not None and time.monotonic() >= stop_at:
                return item, {"error": "stage_deadline"}
            pace()
            result = call(item)
            if "ok" in result:
                break
        return item, result

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        out = dict(pool.map(work, [str(i) for i in items]))
    return out, {"requests": state["requests"], "retries": state["retries"]}


def _limits_record(limits: Any) -> dict[str, Any] | None:
    if not isinstance(limits, Mapping):
        return None
    return {str(stage): {str(k): v for k, v in lim.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
            for stage, lim in limits.items() if isinstance(lim, Mapping)}


def make_limits(concurrency: int | None = None, attempts: int | None = None,
                timeout_s: float | None = None) -> dict[str, dict[str, float]]:
    """DEFAULT_LIMITS with optional overrides applied to every per-symbol
    stage; a value outside LIMIT_BOUNDS is refused."""
    limits = {stage: dict(lim) for stage, lim in DEFAULT_LIMITS.items()}
    for key, value in (("concurrency", concurrency), ("attempts", attempts), ("timeout_s", timeout_s)):
        if value is None:
            continue
        lo, hi = LIMIT_BOUNDS[key]
        if not lo <= value <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}, got {value}")
        for stage in STAGES:
            limits[stage][key] = value
    return limits


_RUNNERS = {"maverick-mcp": ["uv", "run", "python", "-"], "alpaca-mcp": ["python", "-"]}
_MARK = "SCREENER_JSON:"


def _stage_deadline(n_items: int, lim: Mapping[str, float]) -> int:
    """Worst case for a whole stage: every request timing out on every try."""
    per_item = lim["attempts"] * (lim["timeout_s"] + lim["backoff_cap_s"]) + lim["min_interval_s"]
    return int(n_items * per_item / lim["concurrency"]) + 30


def _exec_timeout(n_items: int, lim: Mapping[str, float]) -> int:
    """Host-side ceiling on the container call: the in-container deadline, one
    last request's timeout, and a margin. The in-container script stops by
    itself before this -- the ceiling is the backstop, not the mechanism."""
    return _stage_deadline(n_items, lim) + int(lim["timeout_s"]) + 120


def _run_in(service: str, script: str, timeout: int) -> Any:
    try:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", service, *_RUNNERS[service]],
            input=script, capture_output=True, text=True, cwd=REPO, timeout=timeout, encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        raise ProviderError("stage_timeout") from None
    except OSError:
        raise ProviderError("docker_unavailable") from None
    if result.returncode != 0:
        raise ProviderError("container_failed")  # stderr is deliberately not kept
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(_MARK):
            return json.loads(line[len(_MARK):])
    raise ProviderError("no_output")


def _container_script(child_source: str, items: Sequence[str], lim: Mapping[str, float]) -> str:
    """The script piped into a container: bounded_map over `items`, each
    request a killable child process running `child_source`."""
    return "\n".join([
        "import json", f"CHILD = {child_source!r}", inspect.getsource(bounded_map),
        f"out, stats = bounded_map({list(items)!r}, CHILD, deadline_s={_stage_deadline(len(items), lim)}, **{dict(lim)!r})",
        f"print({_MARK!r} + json.dumps({{'rows': out, 'stats': stats}}))", "",
    ])


def _unwrap(v: Any) -> Any:
    return v["ok"] if isinstance(v, Mapping) and "ok" in v else {"error": _code((v or {}).get("error"))}


class LiveProviders:
    """CURRENT data via the existing containers: yfinance/Wikipedia inside
    maverick-mcp, Alpaca news inside alpaca-mcp. Read-only. `live = True`
    date-locks any screen that uses it to the run's start date (UTC)."""

    live = True

    def __init__(self, limits: Mapping[str, Mapping[str, float]] | None = None):
        self.limits = {stage: dict(lim) for stage, lim in (limits or DEFAULT_LIMITS).items()}

    def _fetch(self, service: str, stage: str, child_source: str, items: Sequence[str]) -> dict[str, Any]:
        lim = self.limits[stage]
        return _run_in(service, _container_script(child_source, items, lim), _exec_timeout(len(items), lim))

    def universe(self) -> dict[str, Any]:
        got = self._fetch("maverick-mcp", "universe", f'''
from io import StringIO
import httpx, pandas as pd
def one(_):
    r = httpx.get({UNIVERSE_URL!r}, headers={{"User-Agent": "Mozilla/5.0 (watchlist-screener)"}},
                  timeout=30, follow_redirects=True)
    r.raise_for_status()
    for t in pd.read_html(StringIO(r.text)):
        col = next((c for c in t.columns if "Symbol" in str(c) or "Ticker" in str(c)), None)
        if col is not None:
            return [str(s).replace(".", "-").strip() for s in t[col]]
    raise LookupError("no constituent table")
''', ["universe"])
        value = got["rows"]["universe"]
        if "ok" not in value:
            raise ProviderError(_code(value.get("error")), got.get("stats"))
        return {"source": UNIVERSE_SOURCE, "url": UNIVERSE_URL, "symbols": value["ok"], "stats": got.get("stats")}

    def facts(self, symbols: Sequence[str]) -> dict[str, Any]:
        got = self._fetch("maverick-mcp", "facts", '''
import yfinance as yf
def one(s):
    i = yf.Ticker(s).info
    return {"name": i.get("shortName"), "sector": i.get("sector"), "industry": i.get("industry"),
            "market_cap": i.get("marketCap"), "avg_volume": i.get("averageVolume"),
            "price": i.get("currentPrice") or i.get("regularMarketPrice")}
''', symbols)
        return {"rows": {s: _unwrap(got["rows"].get(s)) for s in symbols}, "stats": got.get("stats")}

    def history(self, symbols: Sequence[str]) -> dict[str, Any]:
        got = self._fetch("maverick-mcp", "history", '''
import yfinance as yf
def one(s):
    bars = int(len(yf.Ticker(s).history(period="5y")))
    if not bars:  # yfinance returns an empty frame on a failed fetch instead of raising
        raise LookupError("no history returned")
    return {"bars": bars}
''', symbols)
        return {"rows": {s: _unwrap(got["rows"].get(s)) for s in symbols}, "stats": got.get("stats")}

    def news(self, symbols: Sequence[str], windows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
        keys = [f"{s}|{i}" for s in symbols for i in range(len(windows))]
        got = self._fetch("alpaca-mcp", "news", f'''
import asyncio
from server import get_historical_news
WINDOWS = {[dict(w) for w in windows]!r}
def one(key):
    s, i = key.rsplit("|", 1)
    w = WINDOWS[int(i)]
    r = asyncio.run(get_historical_news(s, w["start_at"], w["end_at"]))
    if not r["complete"]:
        return {{"error": "incomplete_pagination"}}  # fail closed; a retry would not help
    return {{"articles": len(r["articles"])}}
''', keys)
        return {"rows": {s: [_unwrap(got["rows"].get(f"{s}|{i}")) for i in range(len(windows))] for s in symbols},
                "stats": got.get("stats")}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def summary(snap: Mapping[str, Any]) -> str:
    reasons: dict[str, int] = {}
    for r in snap["rows"]:
        for code in r["reasons"]:
            reasons[code] = reasons.get(code, 0) + 1
    lines = [
        f"attempt {snap['attempt_id']} ({snap['started_at']} -> {snap['finished_at']}): "
        f"{snap['screen_status'].upper()}"
        + (f" ({', '.join(snap['failure_reasons'])})" if snap["failure_reasons"] else ""),
        f"universe: {snap['universe']['count']} names; " + ", ".join(f"{k} {v}" for k, v in snap["counts"].items()),
        "reasons: " + (", ".join(f"{k} {v}" for k, v in sorted(reasons.items())) or "none"),
    ]
    for name in ("universe", *STAGES):
        t = snap["telemetry"][name]
        lines.append(
            f"{name}: requested {t['requested']}, ok {t['ok']}, unavailable {t['unavailable']}, "
            f"{t['elapsed_s']}s, requests {t['requests']}, retries {t['retries']}"
            + (f", errors {t['errors']}" if t["errors"] else "")
            + (f", STAGE FAILED ({t['stage_error']})" if t["stage_error"] else ""))
    if snap["pool"] is not None:
        lines += [f"pool ({len(snap['pool'])}), effective from {snap['effective_from']}: {' '.join(snap['pool'])}",
                  "sector mix: " + ", ".join(f"{k} {v}" for k, v in sorted(snap["sector_mix"].items()))]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, providers: Providers | None = None,
         clock: Callable[[], datetime] = _utc_now) -> int:
    parser = argparse.ArgumentParser(
        description="Scalable screener, Stage 1. A LIVE screen fetches CURRENT membership and facts, so it "
                    "always screens the UTC date the run starts on: --dry-run on any day (writes nothing), "
                    "--write only on Saturday/Sunday UTC (the weekly refresh, effective the following "
                    "Monday). --usable fetches nothing and may look up any date.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="LIVE, any weekday: run today's (UTC) screen and print it; write nothing")
    mode.add_argument("--write", action="store_true",
                      help="LIVE, Saturday/Sunday UTC only: run today's screen and record the attempt")
    mode.add_argument("--usable", action="store_true",
                      help="NO FETCH: report which stored pool is usable on --on-date")
    parser.add_argument("--screen-date", help="LIVE modes only, optional: must equal today's UTC date "
                                              "(the default). Any other date is refused before any fetch")
    parser.add_argument("--on-date", help="--usable only, required: any YYYY-MM-DD, past or future")
    parser.add_argument("--output-dir", default=str(SNAPSHOT_DIR), help=f"record directory (default {SNAPSHOT_DIR})")
    for flag, key in (("--concurrency", "concurrency"), ("--attempts", "attempts"), ("--request-timeout", "timeout_s")):
        lo, hi = LIMIT_BOUNDS[key]
        parser.add_argument(flag, type=int, help=f"LIVE: per-stage {key} override, {lo}..{hi}")
    args = parser.parse_args(argv)
    out_dir = Path(args.output_dir)

    if args.usable:
        if not args.on_date or args.screen_date:
            parser.error("--usable takes --on-date (and never --screen-date)")
        print(json.dumps(usable_pool(out_dir, date.fromisoformat(args.on_date)), indent=1))
        return 0
    if args.on_date:
        parser.error("--on-date is only for --usable; a live screen is always today's (UTC)")
    started = clock()  # the ONE run-start instant: the screen date is fixed here and never re-checked
    today = started.astimezone(timezone.utc).date()
    day = date.fromisoformat(args.screen_date) if args.screen_date else today
    try:
        require_today(day, today)  # all of this happens before any provider is even constructed
        if args.write:
            require_weekend(day)
        if providers is None:
            providers = LiveProviders(make_limits(args.concurrency, args.attempts, args.request_timeout))
    except ValueError as exc:
        parser.error(str(exc))
    try:
        if args.dry_run:
            snap = run_screen(day, providers, snapshot_dir=out_dir, clock=clock, started=started,
                              attempt=next_attempt(out_dir, screen_id(day, DEFAULT_CONFIG)))
            print(summary(snap) + "\nDRY RUN -- nothing written")
        else:
            snap, reused = screen(day, providers, out_dir, clock=clock, started=started)
            where = result_path(out_dir, snap["screen_id"]) if snap["screen_status"] == SCREEN_OK \
                else attempt_path(out_dir, snap["attempt_id"])
            print(summary(snap) + f"\n{'reused existing result' if reused else 'recorded'} {where}")
    except (SnapshotCorrupt, ScreenConflict) as exc:
        print(f"REFUSED -- stored records are not usable: {exc}", file=sys.stderr)
        return 2
    return 0 if snap["screen_status"] == SCREEN_OK else 1


if __name__ == "__main__":
    sys.exit(main())
