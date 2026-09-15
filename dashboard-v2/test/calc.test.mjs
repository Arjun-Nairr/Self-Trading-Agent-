// Fixture-based checks for js/calc.js. No test framework — plain Node
// assert, run with: node dashboard-v2/test/calc.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  pairTrades, realizedPnl, winRate, maxDrawdown, totalReturn,
  assertRunIsolated, isBlockedRow, rejectionDistribution, STARTING_EQUITY,
  relatedActivity, openPositionDetail, computeHealth, equityCurveSeries,
  ruleLabel, positionsFromTrades, latestPositions, trainingRunsFromRows,
  decisionSummary, runStatusView, relativeTime, RUN_STATUS_STALE_MS,
  isRunStatusDismissed, dismissRunStatus, runStatusDismissKey,
} from "../js/calc.js";
import { STRATEGY_PROFILE, CIRCUIT_BREAKER_NOTE } from "../js/strategy-profile.js";
import { REFLECTION_LIMIT } from "../js/data.js";
import { ROADMAP_SECTORS, STATUS_MARKS, STATUS_LABELS } from "../js/roadmap-data.js";

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

/* -------------------------------------------------------------- health */

test("computeHealth: healthy when latest snapshot is newer than the last failure", () => {
  const snaps = [{ created_at: "2026-01-05T00:00:00Z" }];
  assert.deepEqual(computeHealth(snaps, { created_at: "2026-01-04T00:00:00Z" }), { healthy: true, label: "Healthy" });
  assert.deepEqual(computeHealth(snaps, { created_at: "2026-01-06T00:00:00Z" }), { healthy: false, label: "Needs attention" });
  assert.deepEqual(computeHealth(snaps, null), { healthy: true, label: "Healthy" });
  // a tie reads as "needs attention", never silently healthy
  assert.deepEqual(computeHealth(snaps, { created_at: "2026-01-05T00:00:00Z" }), { healthy: false, label: "Needs attention" });
  assert.equal(computeHealth([], null), null);
  assert.equal(computeHealth(null, null), null);
});

/* ---------------------------------------------------------- equity curve */

test("equityCurveSeries: ascending order, empty/single/multiple snapshots", () => {
  assert.deepEqual(equityCurveSeries([]), []);
  assert.deepEqual(equityCurveSeries(null), []);

  const one = equityCurveSeries([{ day_label: "Day 5", equity: 101000, created_at: "2026-01-05T00:00:00Z" }]);
  assert.equal(one.length, 1);
  assert.equal(one[0].equity, 101000);

  // stored newest-first (matching data.js's own query order) -> series
  // must come back ascending (oldest first) for a left-to-right chart.
  const multi = equityCurveSeries([
    { day_label: "Day 3", equity: 102000, created_at: "2026-01-03T00:00:00Z" },
    { day_label: "Day 2", equity: 99000, created_at: "2026-01-02T00:00:00Z" },
    { day_label: "Day 1", equity: 100000, created_at: "2026-01-01T00:00:00Z" },
  ]);
  assert.deepEqual(multi.map(p => p.label), ["Day 1", "Day 2", "Day 3"]);
  assert.deepEqual(multi.map(p => p.equity), [100000, 99000, 102000]);
});

test("equityCurveSeries: falls back to a formatted timestamp when day_label is absent", () => {
  const series = equityCurveSeries([{ equity: 100000, created_at: "2026-01-01T00:00:00Z" }]);
  assert.equal(series.length, 1);
  assert.ok(series[0].label && series[0].label !== "—");
});

/* ------------------------------------------------------------ rule labels */

test("ruleLabel: maps known §-references, degrades unknown ones safely", () => {
  assert.equal(ruleLabel("§2"), "Position sizing (Rule 2)");
  assert.equal(ruleLabel("§2b"), "Maximum position size (Rule 2b)");
  assert.equal(ruleLabel("§3"), "Available cash check (Rule 3)");
  assert.equal(ruleLabel("§6a"), "Stop placement (Rule 6a)");
  assert.equal(ruleLabel("§6b"), "Minimum reward:risk (Rule 6b)");
  // unknown reference degrades to "Rule <ref without §>", never disappears
  assert.equal(ruleLabel("§9a"), "Rule 9a");
  assert.equal(ruleLabel("§99"), "Rule 99");
});

/* -------------------------------------------------------------- positions */

test("positionsFromTrades: combines open+closed, newest entry first", () => {
  const trades = [
    { ticker: "OPEN1", action: "BUY", status: "filled", fill_price: 10, fill_quantity: 10, created_at: "2026-01-03T00:00:00Z" },
    { ticker: "CLOSED1", action: "SELL", status: "filled", fill_price: 12, fill_quantity: 5, created_at: "2026-01-02T12:00:00Z" },
    { ticker: "CLOSED1", action: "BUY", status: "filled", fill_price: 10, fill_quantity: 5, created_at: "2026-01-01T00:00:00Z" },
  ];
  const positions = positionsFromTrades(trades);
  assert.equal(positions.length, 2);
  assert.equal(positions[0].entry.ticker, "OPEN1"); // newest entry first
  assert.equal(positions[0].kind, "open");
  assert.equal(positions[1].kind, "closed");
});

test("latestPositions: slices to the newest N after pairing, reports the true total", () => {
  const trades = [];
  for (let i = 0; i < 25; i++) {
    trades.push({
      ticker: `T${i}`, action: "BUY", status: "filled", fill_price: 10, fill_quantity: 1,
      created_at: new Date(2026, 0, 1 + i).toISOString(),
    });
  }
  const { positions, total } = latestPositions(trades, 20);
  assert.equal(total, 25);
  assert.equal(positions.length, 20);
  // newest 20 of 25 -> T24 (latest) down through T5, T0-T4 excluded
  assert.equal(positions[0].entry.ticker, "T24");
  assert.equal(positions[19].entry.ticker, "T5");

  const { positions: fewPositions, total: fewTotal } = latestPositions(trades.slice(0, 10), 20);
  assert.equal(fewTotal, 10);
  assert.equal(fewPositions.length, 10); // under the limit -> nothing sliced away
});

/* --------------------------------------------------------- strategy sync */

test("STRATEGY_PROFILE / CIRCUIT_BREAKER_NOTE: values stay synchronized with RULES.md's real text", () => {
  const testDir = dirname(fileURLToPath(import.meta.url));
  const rulesText = readFileSync(join(testDir, "..", "..", "RULES.md"), "utf8");

  const byLabel = Object.fromEntries(STRATEGY_PROFILE.map(p => [p.label, p.value]));

  assert.equal(byLabel["Risk per trade"], "2% of equity");
  assert.match(rulesText, /Risk budget:\s*2\.0% of equity per trade/);

  assert.equal(byLabel["Maximum position"], "20% of equity");
  assert.match(rulesText, /No single position may exceed 20% of equity/);

  assert.equal(byLabel["Portfolio exposure"], "cash only · no margin");
  assert.match(rulesText, /Limited only by available cash\. No margin, ever/);

  assert.equal(byLabel["Stop distance"], "3%–12% below entry");
  assert.match(rulesText, /stop must fall between 3% and 12% below entry/);

  assert.equal(byLabel["Reward:risk floor"], "1.5:1 base · 1.25:1 if escalated");
  assert.match(rulesText, /Base tier: 1\.5:1\./);
  assert.match(rulesText, /Escalated tier: 1\.25:1/);

  assert.equal(byLabel["Time stop"], "5 trading days");
  assert.match(rulesText, /Time stop.*5 trading days/);

  assert.ok(CIRCUIT_BREAKER_NOTE.includes("30%"));
  assert.match(rulesText, /30% from peak equity/);
});

/* ------------------------------------------------------ run classification */

test("trainingRunsFromRows: excludes smoke/archived, keeps training runs with snapshots", () => {
  const registryRows = [
    { run_id: "train-1", run_kind: "training" },
    { run_id: "smoke-1", run_kind: "smoke" },
    { run_id: "archived-1", run_kind: "archived" },
  ];
  const snapshotRows = [
    { run_id: "smoke-1", created_at: "2026-01-05T00:00:00Z", day_label: "Day 3" },
    { run_id: "train-1", created_at: "2026-01-04T00:00:00Z", day_label: "Day 41" },
    { run_id: "archived-1", created_at: "2026-01-03T00:00:00Z", day_label: "Day 179" },
    { run_id: "train-1", created_at: "2026-01-02T00:00:00Z", day_label: "Day 40" }, // dup run_id, older
  ];
  const runs = trainingRunsFromRows(registryRows, snapshotRows);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].run_id, "train-1");
  assert.equal(runs[0].latest_day_label, "Day 41"); // first (newest) occurrence kept, not the dup
});

// Correction pass item E (prior audit M2): backtest_run_registry.run_kind
// gained 'evaluation' (backtest-harness/eval_suite.py's comparison
// episodes) -- an eval episode must never appear in this dashboard's
// training-run selector, exactly like smoke/archived above.
test("trainingRunsFromRows: excludes 'evaluation' runs, same as smoke/archived", () => {
  const registryRows = [
    { run_id: "train-1", run_kind: "training" },
    { run_id: "eval-1", run_kind: "evaluation" },
  ];
  const snapshotRows = [
    { run_id: "eval-1", created_at: "2026-01-05T00:00:00Z", day_label: "Day 20" },
    { run_id: "train-1", created_at: "2026-01-04T00:00:00Z", day_label: "Day 41" },
  ];
  const runs = trainingRunsFromRows(registryRows, snapshotRows);
  assert.equal(runs.length, 1);
  assert.equal(runs[0].run_id, "train-1");
});

test("trainingRunsFromRows: a training run with no snapshots never appears", () => {
  const registryRows = [{ run_id: "train-empty", run_kind: "training" }];
  const runs = trainingRunsFromRows(registryRows, []);
  assert.deepEqual(runs, []);
});

test("trainingRunsFromRows: a run_id with snapshots but no registry row is excluded", () => {
  const registryRows = [];
  const snapshotRows = [{ run_id: "unregistered", created_at: "2026-01-01T00:00:00Z", day_label: "Day 1" }];
  assert.deepEqual(trainingRunsFromRows(registryRows, snapshotRows), []);
});

test("trainingRunsFromRows: empty/null inputs return an empty list, never throw", () => {
  assert.deepEqual(trainingRunsFromRows([], []), []);
  assert.deepEqual(trainingRunsFromRows(null, null), []);
});

/* --------------------------------------------------------- reflections */

test("REFLECTION_LIMIT: reflection retrieval is capped at exactly five", () => {
  assert.equal(REFLECTION_LIMIT, 5);
});

/* ---------------------------------------------------------- decisions */

test("decisionSummary: newest N rows + run-wide status counts, bounded slice", () => {
  const trades = [];
  for (let i = 0; i < 14; i++) {
    trades.push({
      ticker: `T${i}`,
      status: i % 3 === 0 ? "rejected" : i % 3 === 1 ? "filled" : "pending",
      created_at: new Date(2026, 0, 1 + i).toISOString(),
    });
  }
  const { rows, counts } = decisionSummary(trades, 10);
  assert.equal(rows.length, 10);            // capped, never the whole list
  assert.equal(rows[0].ticker, "T13");     // newest first
  assert.equal(rows[9].ticker, "T4");
  assert.equal(counts.total, 14);          // run-wide, not the sliced count
  assert.equal(counts.filled + counts.rejected + counts.pending, 14);

  assert.deepEqual(decisionSummary([], 10), { rows: [], counts: { total: 0, filled: 0, rejected: 0, pending: 0 } });
  assert.deepEqual(decisionSummary(null, 10).counts, { total: 0, filled: 0, rejected: 0, pending: 0 });
});

/* ----------------------------------------------------------- roadmap data */

test("roadmap data: exactly six sectors, one short label per item, public-safe, valid status", () => {
  assert.equal(ROADMAP_SECTORS.length, 6);
  assert.deepEqual(Object.keys(STATUS_MARKS).sort(), ["active", "done", "later", "next"]);
  assert.deepEqual(Object.keys(STATUS_LABELS).sort(), ["active", "done", "later", "next"]);
  const validStatuses = new Set(Object.keys(STATUS_MARKS));
  // no URLs, UUIDs, credentials, or endpoints in any user-visible string
  const forbidden = /(https?:\/\/|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-|password|secret|bearer\s|api[_-]?key|neon\.tech)/i;
  const seenNames = new Set();
  for (const s of ROADMAP_SECTORS) {
    assert.ok(s.name, "sector has a name");
    assert.ok(!seenNames.has(s.name), `sector name ${s.name} is unique`);
    seenNames.add(s.name);
    assert.ok(Array.isArray(s.items) && s.items.length > 0, `sector ${s.name} has at least one item`);
    for (const it of s.items) {
      assert.ok(validStatuses.has(it.status), `sector ${s.name} item has a known status: ${it.status}`);
      assert.equal(typeof it.text, "string");
      assert.ok(it.text.length > 0 && it.text.length < 60, `sector ${s.name} item is one short label: ${it.text}`);
      assert.ok(!forbidden.test(it.text), `sector ${s.name} item stays public-safe: ${it.text}`);
      // one short label only -- never a "<what> — <why>" sentence like the old deferred-lane items
      assert.ok(!it.text.includes(" — "), `sector ${s.name} item carries no embedded reasoning: ${it.text}`);
    }
  }
});

/* ------------------------------------------------------- run-status banner */

const NOW = Date.parse("2026-09-11T12:00:00Z");

test("runStatusView: hidden when there's no status row, or the run isn't training-classified", () => {
  assert.equal(runStatusView({ statusRow: null, isTrainingRun: true, now: NOW }), null);
  assert.equal(
    runStatusView({
      statusRow: { status: "running", current_day: 3, total_days: 10, updated_at: new Date(NOW).toISOString() },
      isTrainingRun: false, // e.g. a smoke run -- registry membership says no, regardless of the row existing
      now: NOW,
    }),
    null,
  );
});

test("runStatusView: 'Training — Day X / Y' for a fresh 'running' row", () => {
  const view = runStatusView({
    statusRow: { status: "running", current_day: 41, total_days: 50, updated_at: new Date(NOW - 60_000).toISOString() },
    isTrainingRun: true, now: NOW,
  });
  assert.deepEqual(view.kind, "training");
  assert.equal(view.label, "Training — Day 41 / 50");
});

test("runStatusView: a smoke run can never read as active training, even with a (hypothetical) fresh row", () => {
  // Belt-and-suspenders check: the harness never writes a status row for a
  // non-training run at all (see run_backtest.py), but the dashboard's own
  // isTrainingRun gate must independently refuse to show one anyway.
  const smokeRunStatusRow = { status: "running", current_day: 2, total_days: 3, updated_at: new Date(NOW).toISOString() };
  assert.equal(runStatusView({ statusRow: smokeRunStatusRow, isTrainingRun: false, now: NOW }), null);
});

test("runStatusView: stale 'running' (beyond the documented threshold) reads as 'Paused'", () => {
  const staleUpdatedAt = new Date(NOW - (RUN_STATUS_STALE_MS + 60_000)).toISOString();
  const view = runStatusView({
    statusRow: { status: "running", current_day: 20, total_days: 50, updated_at: staleUpdatedAt },
    isTrainingRun: true, now: NOW,
  });
  assert.equal(view.kind, "paused");
  assert.match(view.label, /^Paused — last update /);
});

test("runStatusView: 'running' just inside the threshold still reads as 'Training', not 'Paused'", () => {
  const justInside = new Date(NOW - (RUN_STATUS_STALE_MS - 60_000)).toISOString();
  const view = runStatusView({
    statusRow: { status: "running", current_day: 20, total_days: 50, updated_at: justInside },
    isTrainingRun: true, now: NOW,
  });
  assert.equal(view.kind, "training");
});

test("runStatusView: an explicit 'failed' status reads as 'Paused' immediately, even if recently updated", () => {
  const view = runStatusView({
    statusRow: { status: "failed", current_day: 12, total_days: 50, updated_at: new Date(NOW - 1000).toISOString() },
    isTrainingRun: true, now: NOW,
  });
  assert.equal(view.kind, "paused");
});

test("runStatusView: 'completed' reads as 'Complete — <run label>', regardless of staleness", () => {
  const longAgo = new Date(NOW - 999 * 3600_000).toISOString();
  const view = runStatusView({
    statusRow: { status: "completed", current_day: 50, total_days: 50, updated_at: longAgo },
    isTrainingRun: true, runLabel: "e68525f9… · Day 71", now: NOW,
  });
  assert.equal(view.kind, "complete");
  assert.equal(view.label, "Complete — e68525f9… · Day 71");
});

test("relativeTime: minutes/hours/days buckets, never sub-minute", () => {
  assert.equal(relativeTime(0), "just now");
  assert.equal(relativeTime(20_000), "just now");
  assert.equal(relativeTime(5 * 60_000), "5 minutes ago");
  assert.equal(relativeTime(60_000), "1 minute ago");
  assert.equal(relativeTime(3 * 3600_000), "3 hours ago");
  assert.equal(relativeTime(2 * 24 * 3600_000), "2 days ago");
});

test("run-status dismissal: session-only, keyed per run_id, storage failure never hides a live status", () => {
  const store = new Map();
  const fakeStorage = {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, v),
  };
  assert.equal(isRunStatusDismissed(fakeStorage, "run-a"), false);
  dismissRunStatus(fakeStorage, "run-a");
  assert.equal(isRunStatusDismissed(fakeStorage, "run-a"), true);
  // a different run_id is unaffected by dismissing "run-a"
  assert.equal(isRunStatusDismissed(fakeStorage, "run-b"), false);
  assert.equal(store.get(runStatusDismissKey("run-a")), "1");

  // a storage that throws (e.g. disabled/blocked sessionStorage) must
  // degrade to "not dismissed" (fail open, never silently hide a real
  // status), and dismissRunStatus must not throw either
  const throwingStorage = {
    getItem: () => { throw new Error("blocked"); },
    setItem: () => { throw new Error("blocked"); },
  };
  assert.equal(isRunStatusDismissed(throwingStorage, "run-a"), false);
  assert.doesNotThrow(() => dismissRunStatus(throwingStorage, "run-a"));
});

/* --------------------------------------------------------- run isolation */

test("assertRunIsolated: passes for matching/null run_id, throws on mismatch", () => {
  assert.doesNotThrow(() => assertRunIsolated([{ run_id: "r1" }, { run_id: "r1" }], "r1"));
  assert.doesNotThrow(() => assertRunIsolated([{ run_id: null }], "r1")); // live rows carry no run_id
  assert.doesNotThrow(() => assertRunIsolated([{ run_id: "anything" }], null)); // live mode: no expectation
  assert.throws(() => assertRunIsolated([{ run_id: "r1" }, { run_id: "r2" }], "r1"));
});

console.log(`\n${n}/${n} checks passed`);
