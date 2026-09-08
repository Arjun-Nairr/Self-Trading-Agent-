// Pure calculation + formatting functions — no fetch, no DOM. Every metric
// V1 computes is reproduced here unchanged so V2 cannot silently redefine a
// number. Import this module from both the app and the fixture tests.

export const STARTING_EQUITY = 100000;

/* ---------------------------------------------------------------- format */

export const money = n => {
  if (n == null) return "—";
  const v = Number(n);
  const abs = Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (v < 0 ? "-$" : "$") + abs;
};

export const pctSigned = n =>
  (n == null) ? "—" : (n >= 0 ? "+" : "") + Number(n).toFixed(2) + "%";

export const pctPlain = n =>
  (n == null) ? "—" : Number(n).toFixed(2) + "%";

export const time = iso =>
  iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

// Escapes &/</>/"/' — same set V1's esc() covers. Every piece of
// database-supplied text must go through this before it reaches innerHTML.
export const esc = s => (s ?? "").toString().replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------- pairing */

// Pair each BUY with the SELL that closed it. RULES.md §9.1 (never average
// down) guarantees one open lot per ticker at a time, so simple sequential
// pairing per ticker is exact — identical logic to V1's pairTrades().
export function pairTrades(trades) {
  // Sort defensively (newest-first, matching data.js's own
  // order=created_at.desc) rather than trusting caller order — this is
  // exactly the invariant a malformed fixture violated during V2 review.
  const sorted = [...(trades || [])].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
  const byTicker = {};
  for (const t of sorted.reverse()) (byTicker[t.ticker] ||= []).push(t);
  const closed = [], open = [];
  for (const rows of Object.values(byTicker)) {
    let entry = null;
    for (const t of rows) {
      if (t.status !== "filled") continue;
      if (t.action === "BUY") entry = t;
      else if (t.action === "SELL" && entry) {
        const pnl = (Number(t.fill_price) - Number(entry.fill_price)) * Number(entry.fill_quantity);
        closed.push({ entry, exit: t, pnl, pnlPct: (Number(t.fill_price) / Number(entry.fill_price) - 1) * 100 });
        entry = null;
      }
    }
    if (entry) open.push(entry);
  }
  return { closed, open };
}

/* -------------------------------------------------------------- metrics */

export function realizedPnl(closed) {
  return (closed || []).reduce((a, c) => a + c.pnl, 0);
}

export function unrealizedPnl(openPositionsDetail) {
  return (openPositionsDetail || []).reduce((a, p) => a + Number(p.unrealized_pl ?? 0), 0);
}

export function winRate(closed) {
  const n = (closed || []).length;
  if (!n) return { wins: 0, n: 0, pct: null };
  const wins = closed.filter(c => c.pnl > 0).length;
  return { wins, n, pct: (wins / n) * 100 };
}

// Same convention as V1: the worst (most negative) drawdown_pct across the
// scoped snapshot set, floored at 0.
export function maxDrawdown(snapshots) {
  if (!snapshots || !snapshots.length) return 0;
  return Math.min(0, ...snapshots.map(s => Number(s.drawdown_pct ?? 0)));
}

export function totalReturn(equity, startingEquity = STARTING_EQUITY) {
  if (equity == null) return null;
  return (Number(equity) / startingEquity - 1) * 100;
}

// Standardized match for "this activity row is a real risk-check
// rejection" — identical rule to V1's isBlockedRow(): risk_check
// (decision-time) and order_rejected (execution-time) are the two event
// types a rejection is ever logged under, and a real one always cites a
// RULES.md §-number.
export function isBlockedRow(a) {
  return !!a && (a.event_type === "risk_check" || a.event_type === "order_rejected")
    && /§\d/.test(a.description || "");
}

// Distribution of RULES.md sections a set of blocked-activity rows cite.
// Returns entries sorted by count descending, e.g. [["§2b", 37], ...].
export function rejectionDistribution(blockedActivity) {
  const rules = {};
  for (const a of (blockedActivity || [])) {
    if (!isBlockedRow(a)) continue;
    for (const m of (a.description || "").matchAll(/§[0-9]+[a-z]?/g)) rules[m[0]] = (rules[m[0]] || 0) + 1;
  }
  return Object.entries(rules).sort((a, b) => b[1] - a[1]);
}

/* ------------------------------------------------------- activity match */

// Best-effort correlation only — trades rows carry no foreign key into
// agent_activity, so this matches by ticker symbol appearing in the
// description text within a time window of the row's own created_at.
// Never a real join; callers must present it as best-effort. Shared by
// the Trades and Decisions pages so both use the identical rule.
export function relatedActivity(row, activity, windowMs = 48 * 3600_000) {
  if (!row) return [];
  const rowCreated = new Date(row.created_at).getTime();
  return (activity || []).filter(a => {
    if (!a.description || !a.description.includes(row.ticker)) return false;
    return Math.abs(new Date(a.created_at).getTime() - rowCreated) < windowMs;
  });
}

/* --------------------------------------------------- open position detail */

// The one real, current-mark source for an open position: the latest
// scoped snapshot's own open_positions_detail row for that ticker. Never
// fall back to the entry fill price and call it "current" — that silently
// mislabels a stale number as live. Returns null if no matching detail
// exists (e.g. the position opened after the latest snapshot, or the
// snapshot carries no detail for it).
export function openPositionDetail(ticker, snapshot) {
  return (snapshot?.open_positions_detail || []).find(p => p.ticker === ticker) ?? null;
}

/* -------------------------------------------------------------- health */

// Healthy when the latest successful snapshot is newer than the most
// recent failure (or there is no recorded failure at all). null when
// there is no snapshot yet — nothing to assess. A tie (identical
// timestamps) reads as "needs attention", never silently healthy.
export function computeHealth(snapshots, lastFailed) {
  if (!snapshots || !snapshots.length) return null;
  const latestOk = new Date(snapshots[0].created_at).getTime();
  if (!lastFailed) return { healthy: true, label: "Healthy" };
  const latestFail = new Date(lastFailed.created_at).getTime();
  const healthy = latestOk > latestFail;
  return { healthy, label: healthy ? "Healthy" : "Needs attention" };
}

/* ---------------------------------------------------------- equity curve */

// Ascending {label, equity} series from stored snapshots only — no market
// data, no per-trade path. day_label falls back to a formatted timestamp
// when absent (e.g. live mode, which has no simulated day concept).
export function equityCurveSeries(snapshots) {
  return [...(snapshots || [])]
    .slice()
    .reverse()
    .map(s => ({ label: s.day_label || time(s.created_at), equity: Number(s.equity) }))
    .filter(p => Number.isFinite(p.equity));
}

/* --------------------------------------------------------- rule labels */

// Plain-English labels for the RULES.md §-references rejectionDistribution
// surfaces. Internal rules/logs/validators/tests keep their real §
// references unchanged — this is a display-only translation for one page.
const RULE_LABELS = {
  "§2": "Position sizing (Rule 2)",
  "§2b": "Maximum position size (Rule 2b)",
  "§3": "Available cash check (Rule 3)",
  "§6a": "Stop placement (Rule 6a)",
  "§6b": "Minimum reward:risk (Rule 6b)",
};

// Unknown references degrade safely to "Rule <reference without §>" rather
// than disappearing from the UI.
export function ruleLabel(ref) {
  return RULE_LABELS[ref] || `Rule ${(ref || "").replace(/^§/, "")}`;
}

/* ------------------------------------------------------------ positions */

// Open + closed positions combined, newest-first by entry date — the same
// pairing pairTrades() already does, just packaged as one sorted list so
// pages don't each re-implement the merge/sort.
export function positionsFromTrades(trades) {
  const { closed, open } = pairTrades(trades);
  return [
    ...open.map(t => ({ kind: "open", entry: t, exit: null, pnl: null, pnlPct: null })),
    ...closed.map(c => ({ kind: "closed", entry: c.entry, exit: c.exit, pnl: c.pnl, pnlPct: c.pnlPct })),
  ].sort((a, b) => new Date(b.entry.created_at) - new Date(a.entry.created_at));
}

// The newest `limit` reconstructed positions for display, plus the true
// total so a page can label itself "Latest N positions" only when it's
// actually truncating. Never truncates the underlying trade rows
// themselves — callers still hold the full `trades` array for metrics.
export function latestPositions(trades, limit = 20) {
  const all = positionsFromTrades(trades);
  return { positions: all.slice(0, limit), total: all.length };
}

/* ------------------------------------------------------ run classification */

// Registry-backed run discovery: only 'training' runs that actually have
// at least one snapshot, most-recent-first, deduped. 'smoke' and
// 'archived' runs are excluded even if they have snapshots — durable
// classification lives in Neon's backtest_run_registry
// (trade-journal-mcp/schema.sql), not a dashboard-only hidden list. A
// run_id with no registry row at all (should not happen post-migration,
// but never assumed) is excluded too, same as smoke/archived.
export function trainingRunsFromRows(registryRows, snapshotRows) {
  const trainingIds = new Set(
    (registryRows || []).filter(r => r.run_kind === "training").map(r => r.run_id)
  );
  const seen = new Set(), runs = [];
  for (const r of (snapshotRows || [])) {
    if (!r.run_id || !trainingIds.has(r.run_id) || seen.has(r.run_id)) continue;
    seen.add(r.run_id);
    runs.push({ run_id: r.run_id, latest_created_at: r.created_at, latest_day_label: r.day_label });
  }
  return runs;
}

/* --------------------------------------------------------- run isolation */

// Throws if any row carries a run_id other than the expected one. Used
// defensively right after a scoped fetch — a pure function so it is
// directly unit-testable without a network call.
export function assertRunIsolated(rows, expectedRunId) {
  if (expectedRunId == null) return; // live mode: no run concept
  for (const r of (rows || [])) {
    if (r.run_id != null && r.run_id !== expectedRunId) {
      throw new Error(`run isolation violated: expected ${expectedRunId}, saw ${r.run_id}`);
    }
  }
}
