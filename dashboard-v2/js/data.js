import { assertRunIsolated, selectableRunsFromRows } from "./calc.js";

// Decisions page shows only the newest N decision rows — requested at
// this limit, never fetched whole and sliced.
export const DECISIONS_LIMIT = 10;

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
// The Runs page shows only the latest 5 reflections — fetched at
// exactly this limit so there is no client-side overfetch/slice to keep
// in sync with the UI's own cap.
export const REFLECTION_LIMIT = 5;

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
 * Registry-backed run discovery for the selector/default view: 'training'
 * runs (backtest_run_registry.run_kind) that have at least one snapshot,
 * then 'evaluation' runs (shown even with no snapshots, so a Day-1 halt is
 * visible). 'smoke' and 'archived' runs are never returned here, regardless
 * of how much snapshot data they have — see selectableRunsFromRows() for the
 * (independently tested) filter logic. Training runs sort first, so the
 * default is the newest training run whenever one exists.
 * There is deliberately no "latest snapshot, whatever run it belongs to"
 * fallback — an empty result means an honest empty state, never an
 * arbitrary non-training run.
 */
export async function listSelectableRuns() {
  const [registryRows, snapshotRows] = await Promise.all([
    qAll("backtest_run_registry", `?select=run_id,run_kind,created_at`),
    qAll("portfolio_snapshots", `?mode=eq.backtest&select=run_id,created_at,day_label&order=created_at.desc`),
  ]);
  return selectableRunsFromRows(registryRows, snapshotRows);
}

/**
 * The full data bundle one page render needs, scoped strictly to
 * (mode, run_id) — live has no run_id so runFilter is empty, matching V1.
 */
export async function loadRunData(mode, run_id) {
  const m = MODES[mode];
  const runFilter = run_id ? `&run_id=eq.${run_id}` : "";
  const tradeScope = `?execution_mode=eq.${m.trades}${runFilter}`;
  const decisionCount = extra => qCount("trades", `${tradeScope}${extra}`);

  const [
    trades, activity, snapshots, blockedActivity, insights, failedCount, lastFailedRows,
    decisionRows, dTotal, dFilled, dRejected, dPending, runStatusRows,
  ] = await Promise.all([
    qAll("trades", `${tradeScope}&order=created_at.desc`),
    q("agent_activity", `?mode=eq.${m.activity}${runFilter}&order=created_at.desc&limit=200`),
    qAll("portfolio_snapshots", `?mode=eq.${m.snapshots}${runFilter}&order=created_at.desc`),
    qAll("agent_activity", `?mode=eq.${m.activity}${runFilter}&event_type=in.(risk_check,order_rejected)&order=created_at.desc`),
    q("insight_log", `?mode=eq.${m.activity}${runFilter}&order=created_at.desc&limit=${REFLECTION_LIMIT}`),
    qCount("agent_activity", `?mode=eq.${m.activity}${runFilter}&event_type=eq.cycle_failed`),
    q("agent_activity", `?mode=eq.${m.activity}${runFilter}&event_type=eq.cycle_failed&order=created_at.desc&limit=1`),
    // Decisions page: newest N rows only (bounded list query), plus
    // run-wide status totals from count-only queries — the whole trade
    // list is never loaded for this page.
    q("trades", `${tradeScope}&order=created_at.desc&limit=${DECISIONS_LIMIT}`),
    decisionCount(""),
    decisionCount("&status=eq.filled"),
    decisionCount("&status=eq.rejected"),
    decisionCount("&status=eq.pending"),
    // Overview run-status banner: at most one row, for the currently
    // selected backtest run only (this table is backtest-only, has one
    // row per run_id — see trade-journal-mcp/schema.sql). Live mode has
    // no run_id, so there is nothing to fetch and nothing to show.
    run_id ? q("backtest_run_status", `?run_id=eq.${run_id}&limit=1`) : Promise.resolve([]),
  ]);

  // Defense-in-depth: the query above already filters by run_id server-
  // side, but this catches a mixed result immediately rather than letting
  // it silently reach a chart or total.
  assertRunIsolated(trades, run_id);
  assertRunIsolated(snapshots, run_id);
  assertRunIsolated(activity, run_id);
  assertRunIsolated(decisionRows, run_id);

  return {
    trades, activity, snapshots, blockedActivity, insights, failedCount,
    lastFailed: lastFailedRows[0] ?? null,
    decisionRows,
    decisionCounts: { total: dTotal, filled: dFilled, rejected: dRejected, pending: dPending },
    runStatus: runStatusRows[0] ?? null,
  };
}

export const REFRESH_MS = 15000;
