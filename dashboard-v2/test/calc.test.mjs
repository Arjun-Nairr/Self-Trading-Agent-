// Fixture-based checks for js/calc.js. No test framework — plain Node
// assert, run with: node dashboard-v2/test/calc.test.mjs
import assert from "node:assert/strict";
import {
  pairTrades, realizedPnl, winRate, maxDrawdown, totalReturn,
  assertRunIsolated, isBlockedRow, rejectionDistribution, STARTING_EQUITY,
  relatedActivity, openPositionDetail,
} from "../js/calc.js";

let n = 0;
function test(name, fn) { fn(); n++; console.log(`ok - ${name}`); }

/* ------------------------------------------------------------- pairing */

test("pairTrades: pairs one BUY with its SELL, computes P&L", () => {
  const trades = [
    { ticker: "AAA", action: "SELL", status: "filled", fill_price: 12, fill_quantity: 100, created_at: "2026-01-02T00:00:00Z" },
    { ticker: "AAA", action: "BUY", status: "filled", fill_price: 10, fill_quantity: 100, created_at: "2026-01-01T00:00:00Z" },
  ];
  const { closed, open } = pairTrades(trades);
  assert.equal(open.length, 0);
  assert.equal(closed.length, 1);
  assert.equal(closed[0].pnl, 200);
  assert.ok(Math.abs(closed[0].pnlPct - 20) < 1e-9);
});

test("pairTrades: unmatched BUY stays open", () => {
  const trades = [{ ticker: "BBB", action: "BUY", status: "filled", fill_price: 5, fill_quantity: 10, created_at: "2026-01-01T00:00:00Z" }];
  const { closed, open } = pairTrades(trades);
  assert.equal(closed.length, 0);
  assert.equal(open.length, 1);
});

test("pairTrades: non-filled rows (rejected/pending) are ignored", () => {
  const trades = [
    { ticker: "CCC", action: "BUY", status: "rejected", fill_price: null, fill_quantity: null, created_at: "2026-01-01T00:00:00Z" },
    { ticker: "CCC", action: "BUY", status: "pending", fill_price: null, fill_quantity: null, created_at: "2026-01-02T00:00:00Z" },
  ];
  const { closed, open } = pairTrades(trades);
  assert.equal(closed.length, 0);
  assert.equal(open.length, 0);
});

test("pairTrades: correct regardless of input order (sorts defensively)", () => {
  // Same BUY/SELL pair, fed in ascending order (oldest first) instead of
  // the real query's descending order — must still pair correctly.
  const trades = [
    { ticker: "ZZZ", action: "BUY", status: "filled", fill_price: 10, fill_quantity: 50, created_at: "2026-01-01T00:00:00Z" },
    { ticker: "ZZZ", action: "SELL", status: "filled", fill_price: 12, fill_quantity: 50, created_at: "2026-01-02T00:00:00Z" },
  ];
  const { closed, open } = pairTrades(trades);
  assert.equal(open.length, 0);
  assert.equal(closed.length, 1);
  assert.equal(closed[0].pnl, 100);
});

test("pairTrades: empty/null input returns empty result, does not throw", () => {
  assert.deepEqual(pairTrades([]), { closed: [], open: [] });
  assert.deepEqual(pairTrades(null), { closed: [], open: [] });
  assert.deepEqual(pairTrades(undefined), { closed: [], open: [] });
});

/* -------------------------------------------------------------- metrics */

test("realizedPnl: sums closed pnl, 0 for empty", () => {
  assert.equal(realizedPnl([{ pnl: 100 }, { pnl: -40 }]), 60);
  assert.equal(realizedPnl([]), 0);
  assert.equal(realizedPnl(null), 0);
});

test("winRate: computes wins/n/pct, null pct for empty", () => {
  const closed = [{ pnl: 10 }, { pnl: -5 }, { pnl: 20 }];
  const wr = winRate(closed);
  assert.equal(wr.wins, 2);
  assert.equal(wr.n, 3);
  assert.ok(Math.abs(wr.pct - (2 / 3) * 100) < 1e-9);
  assert.deepEqual(winRate([]), { wins: 0, n: 0, pct: null });
});

test("maxDrawdown: worst drawdown_pct across snapshots, floored at 0", () => {
  const snaps = [{ drawdown_pct: -1.2 }, { drawdown_pct: -4.5 }, { drawdown_pct: 0.5 }];
  assert.equal(maxDrawdown(snaps), -4.5);
  assert.equal(maxDrawdown([]), 0);
  // all-positive/no-drawdown snapshots still floor at 0, never a positive "drawdown"
  assert.equal(maxDrawdown([{ drawdown_pct: 0 }]), 0);
});

test("totalReturn: pct vs starting equity, null for null equity", () => {
  assert.ok(Math.abs(totalReturn(110000) - 10) < 1e-9);
  assert.ok(Math.abs(totalReturn(90000, STARTING_EQUITY) - -10) < 1e-9);
  assert.equal(totalReturn(null), null);
});

/* --------------------------------------------------------------- rules */

test("isBlockedRow: requires risk_check/order_rejected + a §-reference", () => {
  assert.equal(isBlockedRow({ event_type: "risk_check", description: "rejected: §2b cap" }), true);
  assert.equal(isBlockedRow({ event_type: "order_rejected", description: "rejected: §6a stop" }), true);
  assert.equal(isBlockedRow({ event_type: "risk_check", description: "no section cited" }), false);
  assert.equal(isBlockedRow({ event_type: "cycle_start", description: "§2b mentioned but wrong type" }), false);
  assert.equal(isBlockedRow(null), false);
});

test("rejectionDistribution: counts §-references, sorted descending", () => {
  const rows = [
    { event_type: "risk_check", description: "§2b cap" },
    { event_type: "risk_check", description: "§2b cap" },
    { event_type: "order_rejected", description: "§6a stop" },
    { event_type: "cycle_start", description: "§2b noise, wrong type" },
  ];
  const dist = rejectionDistribution(rows);
  assert.deepEqual(dist[0], ["§2b", 2]);
  assert.deepEqual(dist[1], ["§6a", 1]);
});

/* ------------------------------------------------------- activity match */

test("relatedActivity: matches by ticker within the time window, both directions", () => {
  const row = { ticker: "ALPHA", created_at: "2026-01-05T12:00:00Z" };
  const activity = [
    { description: "ALPHA rejected: §2b cap", created_at: "2026-01-05T13:00:00Z" }, // within window, after
    { description: "ALPHA reflected on earlier", created_at: "2026-01-04T00:00:00Z" }, // within window, before (36h)
    { description: "ALPHA far away", created_at: "2026-02-01T00:00:00Z" }, // outside window
    { description: "BRAVO rejected: §2b cap", created_at: "2026-01-05T12:30:00Z" }, // wrong ticker
    { description: null, created_at: "2026-01-05T12:10:00Z" }, // no description
  ];
  const related = relatedActivity(row, activity);
  assert.equal(related.length, 2);
});

test("relatedActivity: empty for no row, no activity, or no match", () => {
  assert.deepEqual(relatedActivity(null, [{ description: "x", created_at: "2026-01-01T00:00:00Z" }]), []);
  assert.deepEqual(relatedActivity({ ticker: "AAA", created_at: "2026-01-01T00:00:00Z" }, []), []);
  assert.deepEqual(relatedActivity({ ticker: "AAA", created_at: "2026-01-01T00:00:00Z" }, null), []);
});

/* --------------------------------------------------- open position detail */

test("openPositionDetail: finds by ticker, null when no match or no snapshot", () => {
  const snap = { open_positions_detail: [{ ticker: "ALPHA", current_price: 44.9 }, { ticker: "BRAVO", current_price: 20 }] };
  assert.equal(openPositionDetail("ALPHA", snap).current_price, 44.9);
  assert.equal(openPositionDetail("CHARLIE", snap), null); // no matching detail — never falls back to fill price
  assert.equal(openPositionDetail("ALPHA", null), null); // no snapshot at all
  assert.equal(openPositionDetail("ALPHA", { open_positions_detail: [] }), null);
});

/* --------------------------------------------------------- run isolation */

test("assertRunIsolated: passes for matching/null run_id, throws on mismatch", () => {
  assert.doesNotThrow(() => assertRunIsolated([{ run_id: "r1" }, { run_id: "r1" }], "r1"));
  assert.doesNotThrow(() => assertRunIsolated([{ run_id: null }], "r1")); // live rows carry no run_id
  assert.doesNotThrow(() => assertRunIsolated([{ run_id: "anything" }], null)); // live mode: no expectation
  assert.throws(() => assertRunIsolated([{ run_id: "r1" }, { run_id: "r2" }], "r1"));
});

console.log(`\n${n}/${n} checks passed`);
