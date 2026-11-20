"""Run the weekly screener under its own Langfuse trace (`weekly_screen`).

    python live/trace_screen.py [--state-dir DIR] --write        # the Saturday/Sunday refresh, traced
    python live/trace_screen.py --dry-run                         # any day, writes nothing, traced

Everything after the optional `--state-dir` is passed unchanged to `watchlist/scalable_screener.py`'s own CLI,
which keeps every rule it has (today-only live screens, weekend-only writes, write-once records, reuse of an
existing result). This wrapper only observes: each provider stage becomes a span (`screen.universe|facts|history|
news`) with its real inputs, timing, row counts, errors and request/retry stats, and the root records the
screen's identity and outcome (screen id, attempt id, content/pool hash, effective date, counts, failure reasons,
whether an existing result was reused, what was persisted). The screener file is not modified; its functions are
wrapped for the duration of this one process and restored afterwards. A cycle never runs a screen: it only reads
the published result, and its `selection.universe` span names the screen it consumed.

Tracing is off unless `<state-dir>/tracing.env` holds valid keys (docs/live_tracing_runbook.md).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "watchlist"))

from tracing.controller import code_version, load_tracer  # noqa: E402

DEFAULT_STATE_DIR = Path.home() / ".hermes-trading-agent-live"


def _rows_summary(result: Any) -> dict:
    rows = result.get("rows") if isinstance(result, dict) else None
    if not isinstance(rows, dict):
        return {"rows": None}
    errors: dict[str, int] = {}
    for v in rows.values():
        for probe in (v if isinstance(v, (list, tuple)) else [v]):
            if isinstance(probe, dict) and "error" in probe:
                errors[str(probe["error"])[:60]] = errors.get(str(probe["error"])[:60], 0) + 1
    return {"rows": len(rows), "error_entries": dict(sorted(errors.items(), key=lambda kv: -kv[1])[:10]),
            "stats": result.get("stats")}


class TracedProviders:
    """Transparent wrapper over the screener's providers: same results, same exceptions."""

    def __init__(self, inner: Any, tracer: Any):
        self._inner, self._tr = inner, tracer

    def __getattr__(self, name: str) -> Any:              # `live`, `limits`, ...
        return getattr(self._inner, name)

    def universe(self):
        with self._tr.span("screen.universe", kind="tool") as sp:
            u = self._inner.universe()
            sp.set(output={"source": u.get("source"), "url": u.get("url"), "count": len(u.get("symbols") or []),
                           "stats": u.get("stats")})
            return u

    def _stage(self, name: str, symbols, fn, **extra):
        with self._tr.span(f"screen.{name}", kind="tool",
                           input={"requested": len(symbols), "symbols": list(symbols), **extra}) as sp:
            result = fn()
            sp.set(output=_rows_summary(result))
            return result

    def facts(self, symbols):
        return self._stage("facts", symbols, lambda: self._inner.facts(symbols))

    def history(self, symbols):
        return self._stage("history", symbols, lambda: self._inner.history(symbols))

    def news(self, symbols, windows):
        return self._stage("news", symbols, lambda: self._inner.news(symbols, windows), windows=list(windows))


def outcome(captured: dict, argv: list[str], rc: int) -> dict:
    snap = captured.get("snap")
    if snap is None:
        return {"exit_code": rc, "record": "none: refused or failed before a screen record was built"}
    ok = snap.get("screen_status") == "ok"
    reused = bool(captured.get("reused"))
    persisted = ("nothing written (dry run)" if "--dry-run" in argv else
                 "existing result reused; nothing fetched or written" if reused else
                 "result published (write-once)" if ok else "failed attempt recorded; no result published")
    pool = snap.get("pool")
    return {"exit_code": rc, "persisted": persisted, "reused_existing_result": reused,
            "screen_id": snap.get("screen_id"), "attempt_id": snap.get("attempt_id"),
            "screen_date": snap.get("screen_date"), "screen_status": snap.get("screen_status"),
            "failure_reasons": snap.get("failure_reasons"), "counts": snap.get("counts"),
            "pool_size": len(pool) if pool else 0, "pool_hash": snap.get("pool_hash"),
            "content_hash": snap.get("content_hash"), "effective_from": snap.get("effective_from"),
            "previous_ok_screen": snap.get("previous_ok_screen"), "config_hash": snap.get("config_hash"),
            "config": snap.get("config"), "news_windows": snap.get("news_windows"),
            "universe": snap.get("universe"), "telemetry": snap.get("telemetry"),
            "screener_version": snap.get("screener_version")}


def run(argv: list[str], *, tracer: Any, providers: Any = None, clock: Any = None) -> int:
    import scalable_screener as ss
    captured: dict[str, Any] = {}
    orig_run, orig_screen, orig_live = ss.run_screen, ss.screen, ss.LiveProviders

    def run_screen(*a: Any, **k: Any):
        snap = orig_run(*a, **k)
        captured["snap"] = snap
        return snap

    def screen(*a: Any, **k: Any):
        snap, reused = orig_screen(*a, **k)
        captured.update(snap=snap, reused=reused)
        return snap, reused

    ss.run_screen, ss.screen = run_screen, screen
    ss.LiveProviders = lambda limits: TracedProviders(orig_live(limits), tracer)
    try:
        with tracer.trace("weekly_screen", input={"argv": list(argv)},
                          metadata={"job": "weekly_screen", "independent_of_cycles": True}) as root:
            kw = {"clock": clock} if clock is not None else {}
            rc = ss.main(argv, providers=TracedProviders(providers, tracer) if providers is not None else None, **kw)
            out = outcome(captured, list(argv), rc)
            root.set(output=out, metadata={"screen_id": out.get("screen_id")},
                     level=None if rc == 0 else "WARNING",
                     status_message=None if rc == 0 else f"screener exit {rc}: {out.get('failure_reasons')}")
            return rc
    finally:
        ss.run_screen, ss.screen, ss.LiveProviders = orig_run, orig_screen, orig_live


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    state_dir = DEFAULT_STATE_DIR
    if args[:1] == ["--state-dir"] and len(args) >= 2:
        state_dir, args = Path(args[1]), args[2:]
    tracer = load_tracer(state_dir, release=code_version())
    try:
        return run(args, tracer=tracer)
    finally:
        tracer.close()                                    # never waits (see docs/live_tracing_runbook.md)


if __name__ == "__main__":
    sys.exit(main())
