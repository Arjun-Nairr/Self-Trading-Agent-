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
