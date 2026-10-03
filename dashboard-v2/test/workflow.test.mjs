// Fixture-based checks for the Workflow page model (js/workflow-model.js),
// its public-safe text (js/workflow-text.js) and data boundary (js/data.js).
// All rows below are SYNTHETIC fixtures, not real run data.
// Run: node dashboard-v2/test/workflow.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  buildWorkflowModel, replaySteps, currentStage, boxSummary, workflowRunsFromRows, workflowRunLabel,
  STEP_ORDER, ORDER_PATH, CANDIDATE_PATH, STALE_MS,
} from "../js/workflow-model.js";
import { LAYA, LAYA_FIELDS } from "../js/workflow-text.js";
import { selectableRunsFromRows } from "../js/calc.js";

const here = dirname(fileURLToPath(import.meta.url));
let n = 0;
function test(name, fn) { fn(); n++; console.log(`ok - ${name}`); }

const RUN = "11111111-1111-4111-8111-111111111111";
const INV = "22222222-2222-4222-8222-222222222222";
const C1 = "33333333-3333-4333-8333-333333333331";
const C2 = "33333333-3333-4333-8333-333333333332";
const D1 = "44444444-4444-4444-8444-444444444441";
const T0 = Date.parse("2026-10-03T17:00:00Z");
let id = 0;
function ev(cycle, dayIndex, stage, status, metrics = {}, extra = {}, offsetS = 0, inv = INV) {
  id += 1;
  return { id, run_id: RUN, invocation_id: inv, seq: id, cycle_id: cycle, day_index: dayIndex,
    day_label: dayIndex == null ? null : `Day ${dayIndex + 1}`, market_date: dayIndex == null ? null : `2026-09-${28 + dayIndex}`,
    stage, status, occurred_at: new Date(T0 + (offsetS || id) * 1000).toISOString(), metrics,
    decision_id: null, proposal_key: null, ...extra };
}
function fullDay(cycle, di, { proposals = [], orders = [], inv = INV, end = true } = {}) {
  const e = [
    ev(cycle, di, "cycle", "started", {}, {}, 0, inv),
    ev(cycle, di, "prefill_news", orders.length ? "completed" : "skipped", { queued_buys: orders.length }, {}, 0, inv),
    ev(cycle, di, "execution", orders.length ? "started" : "skipped", { queued_orders: orders.length }, {}, 0, inv),
    ...orders.map(o => ev(cycle, di, "execution", "completed", { action: "BUY", outcome: o.outcome }, { decision_id: o.id }, 0, inv)),
    ev(cycle, di, "positions", "skipped", { monitored: 0 }, {}, 0, inv),
    ev(cycle, di, "population", "completed", { universe: 51, excluded_held_or_pending: 0, eligible: 51, selector_mode: "laya" }, {}, 0, inv),
    ev(cycle, di, "laya", "started", { cards: 51 }, {}, 0, inv),
    ev(cycle, di, "laya", "completed", { cards: 51, scored: 51, unavailable: 0, complete: 51, status: "selected",
      score_min: 0.15, score_median: 0.2, score_max: 0.24 }, {}, 0, inv),
    ev(cycle, di, "shortlist", "completed", { selected: 10, k: 10, shortfall: false, status: "selected" }, {}, 0, inv),
    ev(cycle, di, "evidence", "started", {}, {}, 0, inv),
    ev(cycle, di, "evidence", "completed", { names: 10, news_items: 15, technicals_ok: 10, technicals_errors: 0 }, {}, 0, inv),
    ev(cycle, di, "hermes", "started", {}, {}, 0, inv),
    ev(cycle, di, "hermes", "completed", { decisions: proposals.length }, {}, 0, inv),
    ev(cycle, di, "decision_risk", proposals.length ? "started" : "skipped", { proposals: proposals.length }, {}, 0, inv),
    ...proposals.map((p, i) => ev(cycle, di, "decision_risk", "completed",
      { action: "BUY", outcome: p.outcome, ...(p.codes ? { rule_codes: p.codes } : {}) },
      { decision_id: p.id ?? null, proposal_key: `p${di}-${i}` }, 0, inv)),
  ];
  if (end) e.push(ev(cycle, di, "journal", "completed", { trade_rows: 1, snapshot: true }, {}, 0, inv),
    ev(cycle, di, "cycle", "completed", {}, {}, 0, inv));
  return e;
}
const runStart = () => ev(null, null, "run", "started", { sessions_requested: 2, selector_mode: "laya", fundamentals: "off" }, { cycle_id: null });
const runEnd = (status = "completed") => ev(null, null, "run", status, { sessions_completed: 2, halted: status === "failed" }, { cycle_id: null });
const trade = (id, extra = {}) => ({ decision_id: id, run_id: RUN, ticker: "EMR", action: "BUY", status: "filled",
  created_at: "2026-10-03T17:01:00Z", ...extra });

test("event-sourced replay: truthful order, both paths, links across days", () => {
  id = 0;
  const events = [runStart(),
    ...fullDay(C1, 0, { proposals: [{ outcome: "queued", id: D1 }, { outcome: "rejected", codes: ["§2b", "§6b"] }] }),
    ...fullDay(C2, 1, { orders: [{ id: D1, outcome: "filled" }] }), runEnd()];
  const m = buildWorkflowModel({ runId: RUN, runKind: "smoke", events, trades: [trade(D1)] }, T0 + 3600_000);
  assert.equal(m.status.state, "completed");
  assert.equal(m.days.length, 2);
  const [d1, d2] = m.days;
  assert.deepEqual(STEP_ORDER.slice(0, ORDER_PATH.length), ORDER_PATH.map(s => s.key), "order path first");
  assert.deepEqual(replaySteps(d1).map(s => s.key), STEP_ORDER);
  assert.equal(d1.stages.laya.state, "completed");
  assert.equal(d1.stages.laya.metrics.score_max, 0.24);
  assert.equal(d1.stages.queue_outcome.metrics.queued, 1);
  assert.equal(d1.stages.queue_outcome.metrics.rejected, 1);
  assert.deepEqual(d1.proposals[1], { key: "p0-1", decisionId: null, action: "BUY", outcome: "rejected",
    ruleCodes: ["§2b", "§6b"], at: d1.proposals[1].at, source: "event" });
  assert.equal(d1.stages.execution.state, "skipped");
  assert.equal(d2.stages.queued_in.state, "completed");
  assert.equal(d2.stages.fill_outcome.metrics.filled, 1);
  const link = m.links.get(D1);
  assert.equal(link.queuedDay, "Day 1"); assert.equal(link.resolvedDay, "Day 2"); assert.equal(link.ticker, "EMR");
  assert.ok(boxSummary("laya", d1.stages.laya).includes("51 scored"));
  assert.ok(d1.stages.laya.startedAt && d1.stages.laya.endedAt, "real timings only from events");
});

test("journal-only (pre-telemetry) run: outcomes shown, timings and Laya not recorded", () => {
  const activity = [
    { id: 1, run_id: RUN, cycle_id: C1, event_type: "cycle_start", decision_id: null, created_at: "2026-10-03T17:00:00Z" },
    { id: 2, run_id: RUN, cycle_id: C1, event_type: "reasoning", decision_id: null, created_at: "2026-10-03T17:01:00Z" },
    { id: 3, run_id: RUN, cycle_id: C1, event_type: "order_queued", decision_id: D1, created_at: "2026-10-03T17:01:10Z" },
    { id: 4, run_id: RUN, cycle_id: C2, event_type: "cycle_start", decision_id: null, created_at: "2026-10-03T17:03:00Z" },
    { id: 5, run_id: RUN, cycle_id: C2, event_type: "order_queued", decision_id: D1, created_at: "2026-10-03T17:03:05Z" },
  ];
  const snapshots = [
    { run_id: RUN, cycle_id: C1, day_label: "Day 1", created_at: "2026-10-03T17:02:00Z", open_positions: 0 },
    { run_id: RUN, cycle_id: C2, day_label: "Day 2", created_at: "2026-10-03T17:04:00Z", open_positions: 0 },
  ];
  const m = buildWorkflowModel({ runId: RUN, runKind: "smoke", activity, snapshots,
    trades: [trade(D1, { status: "rejected", rejection_reason: "superseded: already holding" })] }, T0);
  assert.equal(m.status.state, "not_recorded");
  const [d1, d2] = m.days;
  for (const k of ["population", "laya", "shortlist", "evidence"]) assert.equal(d1.stages[k].state, "not_recorded", k);
  assert.equal(d1.stages.hermes.source, "journal");
  assert.equal(d1.stages.hermes.startedAt, null, "no invented timing");
  assert.equal(d1.proposals.length, 1);
  assert.equal(d2.proposals.length, 0, "a second order_queued row is a supersession, not a new proposal");
  assert.equal(d2.orders[0].outcome, "superseded");
  assert.equal(m.links.get(D1).resolvedDay, "Day 2");
});

test("silence is not completion: stale events read as interrupted, recent as running", () => {
  id = 0;
  const events = [runStart(), ...fullDay(C1, 0, { end: false }).slice(0, 6)];
  const last = Math.max(...events.map(e => Date.parse(e.occurred_at)));
  const running = buildWorkflowModel({ runId: RUN, events }, last + 30_000);
  assert.equal(running.status.state, "running");
  assert.equal(running.days[0].state, "running");
  assert.equal(running.days[0].stages.laya.state, "running");
  assert.equal(currentStage(running.days[0]), "laya");
  const stale = buildWorkflowModel({ runId: RUN, events }, last + STALE_MS + 60_000);
  assert.equal(stale.status.state, "interrupted");
  assert.match(stale.status.detail, /not confirmed/);
  assert.equal(stale.days[0].stages.laya.state, "interrupted");
});

test("halted run and a retried day keep attempts distinguishable", () => {
  id = 0;
  const INV2 = "22222222-2222-4222-8222-222222222223";
  const first = [runStart(), ev(C1, 0, "cycle", "started"), ev(C1, 0, "hermes", "started"),
    ev(C1, 0, "hermes", "failed"), ev(C1, 0, "cycle", "failed"), runEnd("failed")];
  const halted = buildWorkflowModel({ runId: RUN, events: first }, T0 + 3600_000);
  assert.equal(halted.status.state, "failed");
  assert.equal(halted.days[0].stages.hermes.state, "failed");
  const retry = [...first, ...fullDay(C1, 0, { inv: INV2 }).map(e => ({ ...e, occurred_at: new Date(T0 + 7200_000 + e.id * 1000).toISOString() }))];
  const m = buildWorkflowModel({ runId: RUN, events: retry }, T0 + 3 * 3600_000);
  assert.equal(m.days.length, 1);
  assert.deepEqual(m.days[0].attempts.map(a => a.state), ["failed", "completed"]);
  assert.equal(m.days[0].stages.hermes.state, "completed", "the latest attempt is shown");
});

test("selector-off and fundamentals-off runs are labelled honestly", () => {
  id = 0;
  const e = [ev(null, null, "run", "started", { selector_mode: "off", fundamentals: "off" }, { cycle_id: null }),
    ev(C1, 0, "cycle", "started"), ev(C1, 0, "population", "completed", { universe: 10, eligible: 10, selector_mode: "off" }),
    ev(C1, 0, "laya", "skipped"), ev(C1, 0, "shortlist", "skipped")];
  const m = buildWorkflowModel({ runId: RUN, events: e }, T0);
  assert.equal(m.runInfo.selectorMode, "off");
  assert.equal(m.runInfo.fundamentals, "off");
  assert.equal(m.days[0].stages.laya.state, "skipped");
});

test("open positions and pending orders at the end are shown, never resolved", () => {
  const m = buildWorkflowModel({ runId: RUN, snapshots: [{ run_id: RUN, cycle_id: C1, day_label: "Day 5",
    created_at: "2026-10-03T17:38:00Z", open_positions: 2 }], trades: [trade(D1, { status: "pending", ticker: "ALGM" })] }, T0);
  assert.equal(m.end.openPositions, 2);
  assert.equal(m.end.pending.length, 1);
  assert.equal(m.end.pending[0].status, "pending");
});

test("workflow run list includes labelled smoke runs; other pages still exclude them", () => {
  const reg = [
    { run_id: "a1", run_kind: "smoke", created_at: "2026-10-03T17:23:00Z" },
    { run_id: "b2", run_kind: "training", created_at: "2026-09-01T00:00:00Z" },
    { run_id: "c3", run_kind: "archived", created_at: "2026-08-01T00:00:00Z" },
  ];
  const snaps = [{ run_id: "a1", day_label: "Day 5", created_at: "2026-10-03T17:38:00Z" },
    { run_id: "b2", day_label: "Day 71", created_at: "2026-09-02T00:00:00Z" }];
  const runs = workflowRunsFromRows(reg, snaps);
  assert.deepEqual(runs.map(r => r.run_id), ["a1", "b2"]);
  assert.equal(workflowRunLabel(runs[0]), "Smoke test · a1… · Day 5");
  assert.ok(!selectableRunsFromRows(reg, snaps).some(r => r.run_kind === "smoke"));
});

test("Laya reference text matches the versioned adapter exactly", () => {
  const src = readFileSync(join(here, "..", "..", "backtest-harness", "laya_adapter.py"), "utf8").replace(/"\s*\n\s*"/g, "");
  assert.ok(src.includes(`QUESTION_FORMAT = "${LAYA.questionFormat}"`));
  assert.ok(src.includes(LAYA.question));
  assert.ok(src.includes(LAYA.criteriaTrue), "criteria 'true' text drifted from laya_adapter.py");
  assert.ok(src.includes(LAYA.criteriaFalse), "criteria 'false' text drifted from laya_adapter.py");
  const cards = readFileSync(join(here, "..", "..", "backtest-harness", "laya_selector.py"), "utf8");
  for (const [field] of LAYA_FIELDS) assert.ok(cards.includes(`"${field}"`), field);
  assert.equal(LAYA_FIELDS.length, 7);
  assert.match(LAYA.meaning, /not a probability of profit/);
});

test("data boundary: workflow queries read only public projections with explicit columns", () => {
  const data = readFileSync(join(here, "..", "js", "data.js"), "utf8");
  const wf = data.slice(data.indexOf("/* ------------------------------------------------------------- workflow */"));
  for (const forbidden of ["backtest_cycle_evidence?", "backtest_run_configuration", "prompt_text", "strategy_notes",
    "description", "alias", "insight_text", "raw_response"]) {
    assert.ok(!new RegExp(`select=[^"\`]*${forbidden}`).test(wf) && !wf.includes(`"${forbidden}`), forbidden);
  }
  for (const table of ["public_workflow_events", "PUBLIC_TRADES", "PUBLIC_AGENT_ACTIVITY", "portfolio_snapshots"]) {
    assert.ok(wf.includes(table), table);
  }
  assert.ok(!/method:\s*["'](POST|PATCH|PUT|DELETE)/i.test(data), "read-only");
});

test("paths contain only major stages, with ownership labels", () => {
  assert.equal(ORDER_PATH.length, 6);
  assert.equal(CANDIDATE_PATH.length, 7);
  assert.equal(CANDIDATE_PATH.find(s => s.key === "hermes").owner, "Hermes · masked");
  assert.equal(CANDIDATE_PATH.find(s => s.key === "laya").owner, "Laya worker");
});

console.log(`\n${n} workflow checks passed`);
