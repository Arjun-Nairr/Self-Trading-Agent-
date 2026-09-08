import { assertRunIsolated } from "./calc.js";

// The ONLY module that talks to Neon. UI/page code must never construct a
// database request itself — it calls the functions below and gets back
// plain objects/arrays.
//
// Auth flow, query helpers and endpoints are reused byte-for-byte from V1
// (`dashboard/index.html`) — same anonymous, read-only Neon Data API, same
// public connection endpoints (public REST endpoints, not write
// credentials — see HANDOFF.md §4/§5 and V1's own footer note).

const AUTH_URL = "https://ep-falling-thunder-b1mr6i56.neonauth.c-5.eu-central-1.aws.neon.tech/neondb/auth";
const DATA_API_URL = "https://ep-falling-thunder-b1mr6i56.apirest.c-5.eu-central-1.aws.neon.tech/neondb/rest/v1";

// One Neon database throughout; mode is a column value, not a separate
// connection — identical mapping to V1.
export const MODES = {
  backtest: { trades: "backtest", activity: "backtest", snapshots: "backtest" },
  live: { trades: "alpaca", activity: "live", snapshots: "live" },
};

let cachedToken = null, tokenExpiresAt = 0;
async function getAnonToken() {
  const now = Date.now() / 1000;
  if (cachedToken && tokenExpiresAt - now > 30) return cachedToken;
  const res = await fetch(`${AUTH_URL}/token/anonymous`);
  if (!res.ok) throw new Error(`token fetch failed: ${res.status}`);
  const d = await res.json();
  cachedToken = d.token; tokenExpiresAt = d.expires_at;
  return cachedToken;
}

async function q(table, params = "") {
  const token = await getAnonToken();
  const res = await fetch(`${DATA_API_URL}/${table}${params}`, { headers: { Authorization: `Bearer ${token}` } });
  if (!res.ok) throw new Error(`${table}: ${res.status}`);
  return res.json();
}

async function qCount(table, params = "") {
  const token = await getAnonToken();
  const res = await fetch(`${DATA_API_URL}/${table}${params}&limit=1`, {
    headers: { Authorization: `Bearer ${token}`, Prefer: "count=exact" },
  });
  if (!res.ok) throw new Error(`${table}: ${res.status}`);
  const range = res.headers.get("content-range"); // "0-0/123"
  return range ? parseInt(range.split("/")[1], 10) || 0 : 0;
}

async function qAll(table, params, pageSize = 1000) {
  const token = await getAnonToken();
  let all = [], offset = 0;
  while (true) {
    const res = await fetch(
      `${DATA_API_URL}/${table}${params}&limit=${pageSize}&offset=${offset}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    if (!res.ok) throw new Error(`${table}: ${res.status}`);
    const page = await res.json();
    all = all.concat(page);
    if (page.length < pageSize) break;
    offset += pageSize;
  }
  return all;
}

// backtest_cycle_evidence is deliberately never queried here — it is
// excluded from the anonymous grant and out of scope for this dashboard
// (HANDOFF.md §8, this task's own security requirements).

/**
 * The run currently "current" for a mode: the latest snapshot's run_id.
 * Live rows never carry one (mode has no run concept) — null in that case.
 */
export async function currentRun(mode) {
  const m = MODES[mode];
  const latest = await q("portfolio_snapshots", `?mode=eq.${m.snapshots}&order=created_at.desc&limit=1`);
  return latest[0]?.run_id ?? null;
}

/**
 * Distinct backtest run_ids, most recent first, derived client-side from
 * snapshot rows (PostgREST's anonymous role has no DISTINCT/aggregate
 * endpoint) — same "no data invented, only reduced" discipline V1 uses for
 * trade pairing.
 */
export async function listBacktestRuns() {
  const rows = await qAll("portfolio_snapshots", `?mode=eq.backtest&select=run_id,created_at,day_label&order=created_at.desc`);
  const seen = new Set(), runs = [];
  for (const r of rows) {
    if (!r.run_id || seen.has(r.run_id)) continue;
    seen.add(r.run_id);
    runs.push({ run_id: r.run_id, latest_created_at: r.created_at, latest_day_label: r.day_label });
  }
  return runs;
}

/**
 * The full data bundle one page render needs, scoped strictly to
 * (mode, run_id) — live has no run_id so runFilter is empty, matching V1.
 */
export async function loadRunData(mode, run_id) {
  const m = MODES[mode];
  const runFilter = run_id ? `&run_id=eq.${run_id}` : "";

  const [trades, activity, snapshots, blockedActivity, insights, failedCount, lastFailedRows] = await Promise.all([
    qAll("trades", `?execution_mode=eq.${m.trades}${runFilter}&order=created_at.desc`),
    q("agent_activity", `?mode=eq.${m.activity}${runFilter}&order=created_at.desc&limit=200`),
    qAll("portfolio_snapshots", `?mode=eq.${m.snapshots}${runFilter}&order=created_at.desc`),
    qAll("agent_activity", `?mode=eq.${m.activity}${runFilter}&event_type=in.(risk_check,order_rejected)&order=created_at.desc`),
    q("insight_log", `?mode=eq.${m.activity}${runFilter}&order=created_at.desc&limit=20`),
    qCount("agent_activity", `?mode=eq.${m.activity}${runFilter}&event_type=eq.cycle_failed`),
    q("agent_activity", `?mode=eq.${m.activity}${runFilter}&event_type=eq.cycle_failed&order=created_at.desc&limit=1`),
  ]);

  // Defense-in-depth: the query above already filters by run_id server-
  // side, but this catches a mixed result immediately rather than letting
  // it silently reach a chart or total.
  assertRunIsolated(trades, run_id);
  assertRunIsolated(snapshots, run_id);
  assertRunIsolated(activity, run_id);

  return { trades, activity, snapshots, blockedActivity, insights, failedCount, lastFailed: lastFailedRows[0] ?? null };
}

export const REFRESH_MS = 15000;
