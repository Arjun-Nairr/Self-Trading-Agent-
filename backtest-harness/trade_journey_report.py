"""
trade_journey_report.py — offline, read-only per-trade journey evaluation
report (Round 2 post-hoc evaluation).

Answers: for each closed trade in a run, what did its daily price/P&L/
stop-target path actually look like while it was open, using ONLY data
already stored in Neon by the harness's own existing snapshot write
(run_backtest.py's `snapshot_positions_detail`, portfolio_snapshots.
open_positions_detail)?

HARD BOUNDARY: human-evaluation-only output, exactly like geometry_scan.py,
counterfactual.py and benchmark_sensitivity.py. Never wired into
run_backtest.py, never read by Hermes, never referenced in AGENTS.md or
any prompt. Read-only SELECTs against `trades` and `portfolio_snapshots`
only -- no write, no new table, no new tool, no new MCP server, no
dashboard change. Real tickers are masked in the OUTPUT report only
(the underlying Neon rows already carry real tickers -- see
schema.sql/HANDOFF.md -- masking is a report-boundary courtesy, not a
new privacy control).

Feasibility basis (read-only Neon audit, this session): 14 of 16 closed
Round 2 trades have complete daily snapshot coverage; 2 are genuine
same-session stop-outs with no intermediate day by construction. See
docs/evaluations/round2_trade_journey.md's own Limitations section for
what this does and does not let you evaluate.

Run: python trade_journey_report.py   (writes docs/evaluations/round2_trade_journey.md)
Test: python test_trade_journey_report.py
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUN_ID = "e68525f9-ce89-4588-a57e-866f28cd99fd"  # completed Round 2 training run
OUT_PATH = REPO / "docs" / "evaluations" / "round2_trade_journey.md"

# Exact literal exit-reason prefixes known to mean a mechanical exit --
# either the harness's own fixed strings (record_exit/simulate_trading_day,
# run_backtest.py) or, for time-stops specifically, a small explicit
# allowlist of the exact discretionary phrasings verified present in the
# real Round 2 report (docs/evaluations/round2_trade_journey.md). An EXACT
# prefix match against a known-good string, never fuzzy keyword matching
# ("stop" alone proves nothing), is what keeps "mechanical_*" trustworthy.
# Any other free-form Hermes prose is deliberately left "unclassified"
# with its raw text preserved rather than guessed at -- extend this list
# only by adding another exact, verified prefix, never a substring/regex.
_MECHANICAL_PREFIXES = [
    ("Mechanical exit: stop hit", "mechanical_stop"),
    ("Mechanical exit: target hit", "mechanical_target"),
    ("Mechanical exit: time stop (", "mechanical_time_stop"),
    ("Closed because the time stop expired", "mechanical_time_stop"),
    ("5-day time stop reached;", "mechanical_time_stop"),
    ("The 5-day time stop has been reached", "mechanical_time_stop"),
]


def classify_exit_reason(notes: str | None) -> str:
    notes = notes or ""
    for prefix, category in _MECHANICAL_PREFIXES:
        if notes.startswith(prefix):
            return category
    return "unclassified"


def pair_trades(trades: list[dict]) -> list[dict]:
    """Same sequential per-ticker BUY->SELL pairing dashboard-v2/js/calc.js's
    pairTrades uses (one open lot per ticker at a time, no overlap) --
    reused here, not reinvented. `trades` sorted ascending by created_at."""
    by_ticker: dict[str, list[dict]] = {}
    for t in sorted(trades, key=lambda t: t["created_at"]):
        by_ticker.setdefault(t["ticker"], []).append(t)
    closed = []
    for rows in by_ticker.values():
        entry = None
        for t in rows:
            if t["status"] != "filled":
                continue
            if t["action"] == "BUY":
                entry = t
            elif t["action"] == "SELL" and entry is not None:
                closed.append({"entry": entry, "exit": t})
                entry = None
    return closed


def build_journey(pair: dict, snapshots: list[dict]) -> dict:
    """Reconstruct one closed trade's daily journey from snapshots already
    written for the run -- no per-trade query, just filtering the run's
    full snapshot list (already one row per trading day regardless of
    which tickers were held), which is what makes `expected` derivable
    from existing data alone.

    Window is [entry fill timestamp, exit decision timestamp) -- half-open,
    keyed on the REAL fill (`updated_at`), not the queued-decision
    timestamp (`created_at`) -- see the module docstring's feasibility
    basis for why created_at on the entry row is wrong here."""
    entry, exit_ = pair["entry"], pair["exit"]
    ticker = entry["ticker"]
    lo, hi = entry["updated_at"], exit_["created_at"]
    window = [s for s in snapshots if lo <= s["created_at"] < hi]

    daily = []
    for s in window:
        detail = next((d for d in (s.get("open_positions_detail") or []) if d.get("ticker") == ticker), None)
        if detail is None:
            continue
        daily.append({
            "day_label": s.get("day_label"),
            "current_price": float(detail["current_price"]),
            "unrealized_pl": float(detail["unrealized_pl"]),
            "unrealized_pl_pct": float(detail["unrealized_pl_pct"]),
            "stop_price": float(detail["stop_price"]) if detail.get("stop_price") is not None else None,
            "target_price": float(detail["target_price"]) if detail.get("target_price") is not None else None,
        })
    for d in daily:
        d["dist_to_stop"] = (d["current_price"] - d["stop_price"]) if d["stop_price"] is not None else None
        d["dist_to_target"] = (d["target_price"] - d["current_price"]) if d["target_price"] is not None else None

    expected, captured = len(window), len(daily)
    if expected == 0:
        coverage = "zero_day"
    elif captured == expected:
        coverage = "full"
    else:
        coverage = "partial"  # not seen in Round 2, but never hidden if it occurs

    fill_price, fill_qty = float(entry["fill_price"]), int(entry["fill_quantity"])
    exit_price = float(exit_["fill_price"])
    pnl = (exit_price - fill_price) * fill_qty
    pnl_pct = (exit_price / fill_price - 1) * 100 if fill_price else 0.0

    return {
        "ticker": ticker,
        "entry_fill_ts": entry["updated_at"],
        "entry_fill_price": fill_price,
        "entry_fill_qty": fill_qty,
        "exit_ts": exit_["created_at"],
        "exit_fill_price": exit_price,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "raw_exit_reason": exit_["strategy_notes"],
        "exit_reason_category": classify_exit_reason(exit_["strategy_notes"]),
        "coverage": coverage,
        "expected_days": expected,
        "captured_days": captured,
        "daily": daily,
        "max_pnl": max((d["unrealized_pl"] for d in daily), default=None),
        "min_pnl": min((d["unrealized_pl"] for d in daily), default=None),
    }


def label_journeys(journeys: list[dict]) -> list[dict]:
    """Stable masked labels, ordered chronologically by entry fill -- the
    report never prints a real ticker."""
    ordered = sorted(journeys, key=lambda j: j["entry_fill_ts"])
    for i, j in enumerate(ordered, 1):
        j["label"] = f"TRADE-{i:02d}"
    return ordered


# ---------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------

def _money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.2f}"


def render_trade_section(j: dict) -> str:
    lines = [f"### {j['label']}", ""]
    lines.append(
        f"- Entry fill: {_money(j['entry_fill_price'])} x {j['entry_fill_qty']} @ {j['entry_fill_ts']}\n"
        f"- Exit fill: {_money(j['exit_fill_price'])} @ {j['exit_ts']}\n"
        f"- Realized P&L: {_money(j['pnl'])} ({j['pnl_pct']:+.2f}%)\n"
        f"- Exit reason: `{j['exit_reason_category']}` — raw: {j['raw_exit_reason']!r}\n"
        f"- Trading-day snapshots while open: {j['captured_days']}/{j['expected_days']} ({j['coverage']})"
    )
    lines.append("")
    if j["coverage"] == "zero_day":
        lines.append("_No intermediate journey — same-session exit, no end-of-day mark was ever recorded while open._")
    else:
        lines.append("| Day | Price | Unrealized P&L | Dist to stop | Dist to target |")
        lines.append("|---|---|---|---|---|")
        for d in j["daily"]:
            lines.append(
                f"| {d['day_label']} | {_money(d['current_price'])} | "
                f"{_money(d['unrealized_pl'])} ({d['unrealized_pl_pct']:+.2f}%) | "
                f"{_money(d['dist_to_stop'])} | {_money(d['dist_to_target'])} |"
            )
        lines.append("")
        lines.append(f"Max daily-close P&L: {_money(j['max_pnl'])} · Min daily-close P&L: {_money(j['min_pnl'])}")
        if j["coverage"] == "partial":
            lines.append("")
            lines.append(f"**WARNING: partial coverage** — {j['expected_days'] - j['captured_days']} "
                          f"trading day(s) this trade was open have no matching snapshot detail.")
    lines.append("")
    return "\n".join(lines)


def render_report(run_id: str, journeys: list[dict], source: str) -> str:
    full = [j for j in journeys if j["coverage"] == "full"]
    zero = [j for j in journeys if j["coverage"] == "zero_day"]
    partial = [j for j in journeys if j["coverage"] == "partial"]
    time_stops = [j for j in journeys if j["exit_reason_category"] == "mechanical_time_stop"]
    ts_winning = [j for j in time_stops if j["pnl"] >= 0]

    out = [
        "# Round 2 per-trade journey evaluation",
        "",
        f"Run: `{run_id}` · Generated: {datetime.now().isoformat(timespec='seconds')} · Source: {source}",
        "",
        "Offline, read-only, human-evaluation-only report (same boundary as "
        "`geometry_scan.py`/`counterfactual.py`/`benchmark_sensitivity.py`). "
        "Never wired into `run_backtest.py`, never read by Hermes. Built "
        "entirely from `trades` and `portfolio_snapshots.open_positions_detail`, "
        "already written by the existing harness — no new capture, no new "
        "table, no new tool. Trade identities are masked labels; see "
        "`trade-journal-mcp/schema.sql` for the real (unmasked) stored rows.",
        "",
        "## Summary",
        "",
        f"- Closed trades: {len(journeys)}",
        f"- Full daily-journey coverage: {len(full)}",
        f"- Zero-day (same-session exit, no intermediate journey by construction): {len(zero)}",
        f"- Partial coverage (should not occur — flagged if it does): {len(partial)}",
        f"- Mechanical time-stop exits: {len(time_stops)} (in-the-money at exit: {len(ts_winning)}, "
        f"underwater: {len(time_stops) - len(ts_winning)})",
        "",
        "## Trades",
        "",
    ]
    for j in journeys:
        out.append(render_trade_section(j))
    out += [
        "## Limitations",
        "",
        "- This evaluates what happened under the actual five-day time stop "
        "as it was run — not what a different time-stop horizon would have "
        "produced.",
        "- It does not prove what a longer hold would have done after the "
        "real exit: no price is stored (or fetched here) for any date after "
        "a trade's actual exit.",
        "- It cannot evaluate alternative stop-distance bands from daily "
        "closes: a stop is evaluated intraday (15-minute bars) against bar "
        "low/high, and only the end-of-day close is stored in "
        "`portfolio_snapshots`.",
        "- No counterfactual claim (a wider/narrower stop, a longer hold) is "
        "supported by this report without separately re-fetching existing "
        "historical intraday bars (`alpaca-mcp`'s `get_intraday_bars`, "
        "already used by `run_backtest.py`'s own `fetch_intraday_bars`) for "
        "the specific dates in question — this report does not do that.",
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------------
# data fetch (read-only) — mirrors benchmark_sensitivity.py's
# fetch_hermes_equity_curve pattern (SELECT-only, run-scoped). That
# pattern proxies the query through the trade-journal container via
# `run_in`, which needs Docker running; when Docker isn't up (as in this
# session), this falls back to the SAME underlying Neon connection
# trade-journal-mcp/server.py's own get_pool() uses (NEON_DATABASE_URL,
# statement_cache_size=0 -- required for the pooled endpoint), just
# invoked host-side instead of proxied through the container. Same
# credentials, same read-only SELECTs, same boundary.
# ---------------------------------------------------------------------

async def _fetch_direct(run_id: str) -> tuple[list[dict], list[dict]]:
    import asyncpg

    dsn = os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        env_path = REPO / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("NEON_DATABASE_URL="):
                    dsn = line.split("=", 1)[1].strip()
                    break
    if not dsn:
        raise RuntimeError("NEON_DATABASE_URL not set (checked env and .env)")

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        trade_rows = await conn.fetch(
            "SELECT ticker, action, status, fill_price, fill_quantity, strategy_notes, "
            "created_at, updated_at FROM trades WHERE run_id=$1 ORDER BY created_at",
            run_id,
        )
        snap_rows = await conn.fetch(
            "SELECT day_label, created_at, open_positions_detail FROM portfolio_snapshots "
            "WHERE run_id=$1 AND mode='backtest' ORDER BY created_at",
            run_id,
        )
    finally:
        await conn.close()

    trades = [dict(r) for r in trade_rows]
    snapshots = []
    for r in snap_rows:
        d = dict(r)
        import json
        detail = d["open_positions_detail"]
        d["open_positions_detail"] = json.loads(detail) if isinstance(detail, str) else (detail or [])
        snapshots.append(d)
    return trades, snapshots


def fetch_run_data(run_id: str) -> tuple[list[dict], list[dict], str]:
    """Returns (trades, snapshots, source_description). Raises RuntimeError
    if no read-only access path is available -- caller decides what to do
    (this script's main() reports that plainly, it never fabricates data)."""
    trades, snapshots = asyncio.run(_fetch_direct(run_id))
    return trades, snapshots, "direct asyncpg (NEON_DATABASE_URL), read-only SELECT"


def main() -> None:
    try:
        trades, snapshots, source = fetch_run_data(RUN_ID)
    except Exception as e:
        print(f"Could not reach Neon read-only: {e}", file=sys.stderr)
        print("No report written. See test_trade_journey_report.py for fixture-verified behavior.", file=sys.stderr)
        sys.exit(1)

    pairs = pair_trades(trades)
    journeys = [build_journey(p, snapshots) for p in pairs]
    journeys = label_journeys(journeys)
    report = render_report(RUN_ID, journeys, source)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(report, encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(journeys)} closed trades: "
          f"{sum(1 for j in journeys if j['coverage'] == 'full')} full, "
          f"{sum(1 for j in journeys if j['coverage'] == 'zero_day')} zero-day, "
          f"{sum(1 for j in journeys if j['coverage'] == 'partial')} partial)")


if __name__ == "__main__":
    main()
